"""Offline tests for twin-pipeline helpers and optional UN validation."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

from tests.pipeline_test_utils import load_pipeline_module
from sdg_pipeline.errors import SourceValidationError


pipeline = load_pipeline_module(
    "fetch_sdg_15_a_1_15_b_1", "fetch_sdg_15_a_1_15_b_1.py"
)


def un_body(rows) -> bytes:
    """Return a minimal official-API-shaped response."""

    return json.dumps({"data": rows}).encode()


def un_row(year: int, value: str) -> dict[str, object]:
    """Return one small United States DC_ODA_BDVDL observation."""

    return {
        "series": "DC_ODA_BDVDL",
        "refArea": "840",
        "timePeriodStart": float(year),
        "value": value,
    }


class TwinBiodiversityPipelineTests(unittest.TestCase):
    """Protect optional validation and audit-output transparency."""

    def test_identical_un_duplicates_are_safely_collapsed(self) -> None:
        values, warnings = pipeline.parse_un_response(
            un_body([un_row(2022, "777.104871"), un_row(2022, "777.104871")])
        )

        self.assertEqual({2022: Decimal("777.104871")}, values)
        self.assertEqual(1, len(warnings))
        self.assertIn("identical duplicate", warnings[0])

    def test_conflicting_un_duplicates_are_rejected(self) -> None:
        with self.assertRaisesRegex(SourceValidationError, "conflicting duplicate"):
            pipeline.parse_un_response(
                un_body([un_row(2022, "777.104871"), un_row(2022, "777.1")])
            )

    def test_audit_rows_preserve_principal_and_significant_values(self) -> None:
        source = pipeline.oecd.OecdResult(
            observations=(),
            source_organization="OECD",
            source_dataset="Rio Markers fixture",
            source_url="https://example.invalid/oecd",
            retrieval_method="api",
            retrieval_date="2026-08-11",
            dataflow_id="DSD_RIOMRKR@DF_RIOMARKERS",
            dataflow_version="1.6",
        )
        calculated = [
            pipeline.indicator.BiodiversityOdaYear(
                year=2022,
                principal=Decimal("545.313317"),
                significant=Decimal("231.791554"),
                combined=Decimal("777.104871"),
            )
        ]

        row = pipeline.build_audit_rows(calculated, source)[0]

        self.assertEqual("545.313317", row["principal_amount"])
        self.assertEqual("231.791554", row["significant_amount"])
        self.assertEqual("777.104871", row["combined_amount"])
        self.assertEqual("2024", row["base_period"])
        self.assertEqual("commitments", row["flow_type"])


if __name__ == "__main__":
    unittest.main()
