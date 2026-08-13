"""Offline tests for the coordinated OECD donor-finance batch script."""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from tests.pipeline_test_utils import load_pipeline_module
from sdg_pipeline.errors import SourceValidationError
from sdg_pipeline.indicators import oecd_donor_finance as indicator
from sdg_pipeline.sources.oecd import OecdResult


pipeline = load_pipeline_module(
    "fetch_oecd_donor_finance_batch", "fetch_oecd_donor_finance_batch.py"
)


def un_row(year: int, value: str, series: str = "DC_TRF_TOTDL") -> dict[str, object]:
    """Return one small official-API-shaped UN row."""

    return {
        "series": series,
        "refArea": "840",
        "timePeriodStart": float(year),
        "value": value,
    }


def source(url: str) -> OecdResult:
    """Build controlled OECD provenance."""

    return OecdResult(
        observations=(),
        source_organization="OECD",
        source_dataset="fixture",
        source_url=url,
        retrieval_method="api",
        retrieval_date="2026-08-12",
        dataflow_id="fixture",
        dataflow_version="1.0",
    )


class QueryTests(unittest.TestCase):
    """Protect verified dataflows, versions, measures, and price bases."""

    def test_verified_query_definitions(self) -> None:
        aid = pipeline.build_aid_for_trade_query()
        dac1 = pipeline.build_dac1_query()
        dac2a = pipeline.build_dac2a_query()

        self.assertEqual("1.6", aid.version)
        self.assertIn("210+220+230+240+250+310+320+331+332", aid.key)
        self.assertIn(".C+D.Q.", aid.key)
        self.assertEqual("1.7", dac1.version)
        self.assertEqual("USA._Z.1+5+1010..1140.USD.V", dac1.key)
        self.assertEqual("1.6", dac2a.version)
        self.assertEqual("USA.LDC.106+206.USD.V", dac2a.key)


class OptionalUnTests(unittest.TestCase):
    """Protect duplicate handling for every optional UN comparison series."""

    def test_identical_duplicates_are_collapsed(self) -> None:
        body = json.dumps(
            {"data": [un_row(2022, "228696.45"), un_row(2022, "228696.45")]}
        ).encode()

        values, warnings = pipeline.parse_un_response(body, "DC_TRF_TOTDL")

        self.assertEqual({2022: Decimal("228696.45")}, values)
        self.assertEqual(1, len(warnings))

    def test_conflicting_duplicates_are_rejected(self) -> None:
        body = json.dumps(
            {"data": [un_row(2022, "228696.45"), un_row(2022, "1")]}
        ).encode()

        with self.assertRaisesRegex(SourceValidationError, "conflicting duplicate"):
            pipeline.parse_un_response(body, "DC_TRF_TOTDL")


class AuditAndOutputTests(unittest.TestCase):
    """Protect audit transparency and six-file failure safety."""

    def test_oda_gni_audit_preserves_all_numerators_and_denominator(self) -> None:
        calculated = [
            indicator.OdaGniYear(
                year=2022,
                net_oda=Decimal("20"),
                gni=Decimal("1000"),
                ldc_bilateral_net_oda=Decimal("5"),
                ldc_imputed_multilateral_oda=Decimal("2"),
                total_percent=Decimal("2"),
                ldc_percent=Decimal("0.7"),
                grant_equivalent_oda=Decimal("22"),
                grant_equivalent_percent=Decimal("2.2"),
            )
        ]

        row = pipeline.build_oda_gni_audit_rows(
            calculated,
            source("https://example.invalid/dac1"),
            source("https://example.invalid/dac2a"),
            source("https://example.invalid/ge"),
        )[0]

        self.assertEqual("20", row["net_oda"])
        self.assertEqual("1000", row["gni"])
        self.assertEqual("5", row["bilateral_net_ldc_oda"])
        self.assertEqual("2", row["imputed_multilateral_ldc_oda"])
        self.assertEqual("22", row["grant_equivalent_oda"])
        self.assertEqual("0.2", row["grant_equivalent_minus_net_percent"])

    def test_first_replace_failure_preserves_all_previous_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            standard_paths = {
                key: root / f"standard-{key}.csv"
                for key in ("8.a.1", "10.b.1", "17.2.1")
            }
            audit_paths = {
                key: root / f"audit-{key}.csv"
                for key in ("8.a.1", "10.b.1", "17.2.1")
            }
            for path in (*standard_paths.values(), *audit_paths.values()):
                path.write_text("previous successful output\n", encoding="utf-8")

            standard_row = indicator.build_resource_flows_standardized(
                [indicator.ResourceFlowYear(2022, Decimal("1"))],
                source("https://example.invalid/oecd"),
            )[0]
            standardized = {key: [standard_row] for key in standard_paths}
            audit_rows = [{column: "" for column in pipeline.AID_FOR_TRADE_AUDIT_COLUMNS}]
            resource_audit = [
                {column: "" for column in pipeline.RESOURCE_FLOWS_AUDIT_COLUMNS}
            ]
            oda_audit = [{column: "" for column in pipeline.ODA_GNI_AUDIT_COLUMNS}]

            with mock.patch.object(pipeline, "STANDARDIZED_PATHS", standard_paths), mock.patch.object(
                pipeline, "AUDIT_PATHS", audit_paths
            ), mock.patch(
                "sdg_pipeline.output.os.replace",
                side_effect=OSError("simulated first replace failure"),
            ):
                with self.assertRaises(OSError):
                    pipeline.write_outputs(
                        standardized, audit_rows, resource_audit, oda_audit
                    )

            for path in (*standard_paths.values(), *audit_paths.values()):
                self.assertEqual(
                    "previous successful output\n", path.read_text(encoding="utf-8")
                )


if __name__ == "__main__":
    unittest.main()
