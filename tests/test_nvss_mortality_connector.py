"""Focused offline tests for the reusable CDC/NVSS mortality connector."""

from __future__ import annotations

import unittest
from decimal import Decimal

from tests.pipeline_test_utils import SRC_ROOT  # Makes src importable.
from sdg_pipeline.errors import RetrievalError, SourceValidationError
from sdg_pipeline.sources import nvss_mortality as nvss


def query(start=2022, end=2023) -> nvss.MortalityQuery:
    return nvss.MortalityQuery("test", ("X40", "X43"), start, end)


def xml_rows(rows: str) -> bytes:
    return (
        '<?xml version="1.0"?><page><data-table>'
        + rows
        + "</data-table></page>"
    ).encode()


def result_for(
    queries, date="2026-08-13", method="test"
) -> nvss.NvssMortalityResult:
    observations = {}
    for item in queries:
        observations[item.key] = tuple(
            nvss.MortalityObservation(
                year=year,
                deaths=100 + year,
                population=1_000_000,
                crude_rate=nvss.calculate_crude_rate(100 + year, 1_000_000),
                source_reported_crude_rate=Decimal("1.0"),
                icd10_selection=item.icd10_selection,
                disaggregation={},
                suppression_status="not_suppressed",
                source_notes=("fixture",),
            )
            for year in item.required_years
        )
    return nvss.NvssMortalityResult(
        observations=observations,
        source_organization=nvss.SOURCE_ORGANIZATION,
        source_dataset=nvss.SOURCE_DATASET,
        source_url="https://wonder.cdc.gov/test",
        retrieval_method=method,
        retrieval_date=date,
    )


class NvssMortalityConnectorTests(unittest.TestCase):
    def test_valid_xml_parsing_calculates_crude_rate_from_inputs(self) -> None:
        body = xml_rows(
            '<r><c l="2022"/><c v="100"/><c v="2,000,000"/>'
            '<c v="5.0"/></r>'
            '<r><c l="2023"/><c v="120"/><c v="2,000,000"/>'
            '<c v="6.0"/></r>'
        )

        rows = nvss.parse_wonder_xml(body, query())

        self.assertEqual(2, len(rows))
        self.assertEqual(100, rows[0].deaths)
        self.assertEqual(2_000_000, rows[0].population)
        self.assertEqual(Decimal("5"), rows[0].crude_rate)
        self.assertEqual("not_suppressed", rows[0].suppression_status)

    def test_request_preserves_exact_icd_filter(self) -> None:
        body = nvss.build_wonder_request_xml(query(2022, 2022)).decode()

        self.assertIn("X40", body)
        self.assertIn("X43", body)
        self.assertIn("O_V2_fmode", body)
        self.assertIn("fadv", body)

    def test_suppressed_observation_is_never_inferred(self) -> None:
        body = xml_rows(
            '<r><c l="2022"/><c v="Suppressed"/><c v="2,000,000"/>'
            '<c v="Suppressed"/></r>'
        )

        rows = nvss.parse_wonder_xml(body, query(2022, 2022))

        self.assertEqual("suppressed", rows[0].suppression_status)
        self.assertIsNone(rows[0].deaths)
        self.assertIsNone(rows[0].population)
        self.assertIsNone(rows[0].crude_rate)

    def test_missing_required_year_is_rejected(self) -> None:
        body = xml_rows(
            '<r><c l="2022"/><c v="100"/><c v="2,000,000"/>'
            '<c v="5.0"/></r>'
        )

        with self.assertRaisesRegex(SourceValidationError, "missing required"):
            nvss.parse_wonder_xml(body, query())

    def test_malformed_or_html_response_is_rejected(self) -> None:
        with self.assertRaisesRegex(SourceValidationError, "HTML"):
            nvss.parse_wonder_xml(b"<!doctype html><html>maintenance</html>", query())
        with self.assertRaisesRegex(SourceValidationError, "malformed"):
            nvss.parse_wonder_xml(b"<page>", query())

    def test_duplicate_or_conflicting_year_is_rejected(self) -> None:
        row = (
            '<r><c l="2022"/><c v="100"/><c v="2,000,000"/>'
            '<c v="5.0"/></r>'
        )
        with self.assertRaisesRegex(SourceValidationError, "duplicate/conflicting"):
            nvss.parse_wonder_xml(xml_rows(row + row), query(2022, 2022))

    def test_api_fetch_records_provenance(self) -> None:
        body = xml_rows(
            '<r><c l="2022"/><c v="100"/><c v="2,000,000"/>'
            '<c v="5.0"/></r>'
        )
        result = nvss.fetch_from_wonder_api(
            [query(2022, 2022)],
            "2026-08-13",
            request_executor=lambda _request, _url: (body, "text/html"),
        )

        self.assertEqual("cdc_wonder_api", result.retrieval_method)
        self.assertEqual(nvss.SOURCE_ORGANIZATION, result.source_organization)
        self.assertEqual(nvss.WONDER_QUERY_URL, result.source_url)

    def test_api_failure_uses_controlled_fallback(self) -> None:
        calls = []

        def api(_queries, _date):
            calls.append("api")
            raise RetrievalError("simulated API outage")

        def fallback(queries, date):
            calls.append("fallback")
            return result_for(queries, date, "cdc_wonder_tsv_fallback")

        result = nvss.fetch_mortality_batch(
            [query()],
            retrieval_date="2026-08-13",
            api_fetcher=api,
            fallback_fetcher=fallback,
        )

        self.assertEqual(["api", "fallback"], calls)
        self.assertEqual("cdc_wonder_tsv_fallback", result.retrieval_method)

    def test_both_source_paths_failing_is_clear(self) -> None:
        def failure(_queries, _date):
            raise RetrievalError("simulated outage")

        with self.assertRaisesRegex(RetrievalError, "both failed"):
            nvss.fetch_mortality_batch(
                [query()], api_fetcher=failure, fallback_fetcher=failure
            )

    def test_official_tsv_parsing(self) -> None:
        body = (
            '"Notes"\t"Year"\t"Year Code"\tDeaths\tPopulation\tCrude Rate\n'
            '\t"2022"\t"2022"\t100\t2000000\t5.0\n'
            '\t"2023"\t"2023"\t120\t2000000\t6.0\n'
            '"Total"\t\t\t220\t4000000\t5.5\n'
        ).encode()

        rows = nvss.parse_wonder_tsv(body, query())

        self.assertEqual([2022, 2023], [row.year for row in rows])
        self.assertEqual(Decimal("6"), rows[-1].crude_rate)

    def test_session_tsv_fallback_sequence_and_provenance(self) -> None:
        tsv = (
            '"Notes"\t"Year"\t"Year Code"\tDeaths\tPopulation\tCrude Rate\n'
            '\t"2022"\t"2022"\t100\t2000000\t5.0\n'
            '"Total"\t\t\t100\t2000000\t5.0\n'
        ).encode()
        responses = iter(
            [
                (
                    b'<form action="/controller/datarequest/D158;jsessionid=SAFE123">',
                    "text/html",
                ),
                (b"<html><title>Results</title> Results</html>", "text/html"),
                (tsv, "text/html"),
            ]
        )
        requests = []

        def executor(request, display_url):
            requests.append((request.full_url, display_url))
            return next(responses)

        result = nvss.fetch_from_wonder_tsv(
            [query(2022, 2022)],
            "2026-08-13",
            request_executor=executor,
            inter_query_delay_seconds=0,
        )

        self.assertEqual(3, len(requests))
        self.assertEqual("cdc_wonder_tsv_fallback", result.retrieval_method)
        self.assertEqual(nvss.WONDER_PAGE_URL, result.source_url)
        self.assertEqual(100, result.observations["test"][0].deaths)


if __name__ == "__main__":
    unittest.main()
