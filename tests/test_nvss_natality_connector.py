"""Focused offline tests for the reusable CDC/NVSS natality connector."""

from __future__ import annotations

import unittest
import io
import zipfile
from decimal import Decimal

from tests.pipeline_test_utils import SRC_ROOT  # Makes src importable.
from sdg_pipeline.errors import RetrievalError, SourceValidationError
from sdg_pipeline.sources import nvss_natality as nvss


def attendant_query(start=2024, end=2024):
    return nvss.NatalityQuery(
        "attendant",
        nvss.MEDICAL_ATTENDANT_VARIABLE,
        tuple(nvss.MEDICAL_ATTENDANT_LABELS),
        False,
        start,
        end,
    )


def age_query(start=2024, end=2024):
    return nvss.NatalityQuery(
        "age",
        nvss.MATERNAL_AGE_9_VARIABLE,
        tuple(nvss.ADOLESCENT_AGE_LABELS),
        True,
        start,
        end,
    )


def xml_rows(rows: str) -> bytes:
    return (f'<?xml version="1.0"?><page><data-table>{rows}</data-table></page>').encode()


def attendant_xml(year=2024) -> bytes:
    rows = "".join(
        f'<r><c l="{year}" v="{year}"/><c l="{label}" v="{code}"/>'
        f'<c v="{1000 + int(code)}"/></r>'
        for code, label in nvss.MEDICAL_ATTENDANT_LABELS.items()
    )
    return xml_rows(rows)


def source_result(queries, date="2026-08-13", method="fixture"):
    observations = {}
    for query in queries:
        if query.dimension == nvss.MEDICAL_ATTENDANT_VARIABLE:
            observations[query.key] = nvss.parse_wonder_xml(
                attendant_xml(query.start_year), query
            )
        else:
            rows = "".join(
                f'<r><c l="{query.start_year}" v="{query.start_year}"/>'
                f'<c l="{label}" v="{code}"/><c v="100"/>'
                f'<c v="500000"/><c v="0.2"/></r>'
                for code, label in nvss.ADOLESCENT_AGE_LABELS.items()
            )
            observations[query.key] = nvss.parse_wonder_xml(xml_rows(rows), query)
    return nvss.NvssNatalityResult(
        observations=observations,
        source_organization=nvss.SOURCE_ORGANIZATION,
        source_dataset=nvss.SOURCE_DATASET,
        source_url=nvss.WONDER_QUERY_URL,
        retrieval_method=method,
        retrieval_date=date,
    )


class NvssNatalityConnectorTests(unittest.TestCase):
    def test_attendant_xml_parsing_and_category_mapping(self):
        rows = nvss.parse_wonder_xml(attendant_xml(), attendant_query())

        self.assertEqual(6, len(rows))
        self.assertEqual(set(nvss.MEDICAL_ATTENDANT_LABELS), {row.category_code for row in rows})
        self.assertEqual("Doctor of Medicine (MD)", rows[0].category_label)
        self.assertTrue(all(row.female_population is None for row in rows))

    def test_age_xml_preserves_births_official_female_population_and_rate(self):
        body = xml_rows(
            '<r><c l="2024" v="2024"/><c l="Under 15 years" v="15"/>'
            '<c v="1500"/><c v="10000000"/><c v="0.15"/></r>'
            '<r><c l="2024" v="2024"/><c l="15-19 years" v="15-19"/>'
            '<c v="130000"/><c v="10000000"/><c v="13.0"/></r>'
        )

        rows = nvss.parse_wonder_xml(body, age_query())

        self.assertEqual(10_000_000, rows[0].female_population)
        self.assertEqual(Decimal("0.15"), rows[0].source_reported_fertility_rate)
        self.assertIn("Census denominator", " ".join(rows[0].source_notes))

    def test_missing_annual_category_is_rejected(self):
        incomplete = attendant_xml().replace(
            b'<r><c l="2024" v="2024"/><c l="Unknown or Not Stated" v="9"/><c v="1009"/></r>',
            b"",
        )
        with self.assertRaisesRegex(SourceValidationError, "missing required"):
            nvss.parse_wonder_xml(incomplete, attendant_query())

    def test_malformed_and_missing_measure_input_is_rejected(self):
        with self.assertRaisesRegex(SourceValidationError, "malformed"):
            nvss.parse_wonder_xml(b"<page>", attendant_query())
        missing_births = xml_rows(
            '<r><c l="2024" v="2024"/><c l="Doctor of Medicine (MD)" v="1"/></r>'
        )
        with self.assertRaisesRegex(SourceValidationError, "missing required"):
            nvss.parse_wonder_xml(missing_births, attendant_query())

    def test_official_tsv_parsing(self):
        lines = ["Year\tYear Code\tMedical Attendant\tMedical Attendant Code\tBirths"]
        for code, label in nvss.MEDICAL_ATTENDANT_LABELS.items():
            lines.append(f"2024\t2024\t{label}\t{code}\t1,000")
        lines.append("Total\t\t\t\t6,000")

        rows = nvss.parse_wonder_tsv("\n".join(lines).encode(), attendant_query())

        self.assertEqual(6_000, sum(row.births or 0 for row in rows))

    def test_api_fetch_records_exact_provenance(self):
        consent = (
            b'<form action="/controller/datarequest/D66;jsessionid=SAFE">'
            b'<input name="O_precision" value="9"></form>'
        )
        responses = iter([(consent, "text/html"), (attendant_xml(), "text/html")])
        result = nvss.fetch_from_wonder_api(
            [attendant_query()],
            "2026-08-13",
            request_executor=lambda _request, _url: next(responses),
            initial_query_delay_seconds=0,
            inter_query_delay_seconds=0,
        )

        self.assertEqual("cdc_wonder_api", result.retrieval_method)
        self.assertEqual(nvss.SOURCE_ORGANIZATION, result.source_organization)
        self.assertEqual(nvss.WONDER_QUERY_URL, result.source_url)

    def test_api_failure_uses_official_fallback(self):
        calls = []

        def api(_queries, _date):
            calls.append("api")
            raise RetrievalError("temporary outage")

        def fallback(queries, date):
            calls.append("fallback")
            return source_result(queries, date, "cdc_wonder_tsv_fallback")

        result = nvss.fetch_natality_batch(
            [attendant_query()],
            retrieval_date="2026-08-13",
            api_fetcher=api,
            fallback_fetcher=fallback,
        )

        self.assertEqual(["api", "fallback"], calls)
        self.assertEqual("cdc_wonder_tsv_fallback", result.retrieval_method)

    def test_public_use_fixed_width_and_census_population_parsing(self):
        def record(age, attendant, residence="1"):
            value = bytearray(b" " * 500)
            value[74:76] = f"{age:02d}".encode()
            value[103:104] = residence.encode()
            value[432:433] = attendant.encode()
            return bytes(value) + b"\n"

        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr(
                "Nat2024PublicUS.c20250501.r20250507.txt",
                record(14, "1") + record(19, "4") + record(25, "9")
                + record(18, "5", residence="4"),
            )
        attendants, ages = nvss._public_use_rows(archive_bytes.getvalue(), 2024)
        population_csv = (
            "SEX,AGE,POPESTIMATE2024\n"
            + "".join(f"2,{age},100\n" for age in range(10, 20))
        ).encode()
        populations = nvss._population_rows(population_csv, 2024)

        self.assertEqual(3, sum(attendants.values()))
        self.assertEqual(1, attendants["1"])
        self.assertEqual({"15": 1, "15-19": 1}, ages)
        self.assertEqual({"15": 500, "15-19": 500}, populations)

    def test_public_use_rejects_malformed_records_and_incomplete_population(self):
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("bad.txt", b"too short\n")
        with self.assertRaisesRegex(SourceValidationError, "too short"):
            nvss._public_use_rows(archive_bytes.getvalue(), 2024)
        with self.assertRaisesRegex(SourceValidationError, "lacks complete"):
            nvss._population_rows(
                b"SEX,AGE,POPESTIMATE2024\n2,10,100\n", 2024
            )


if __name__ == "__main__":
    unittest.main()
