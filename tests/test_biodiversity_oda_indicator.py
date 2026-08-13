"""Offline tests for the shared SDG 15.a.1/15.b.1 transformation."""

from __future__ import annotations

import unittest
from decimal import Decimal

from tests.pipeline_test_utils import SRC_ROOT  # Makes the src package importable.
from sdg_pipeline.indicators import biodiversity_oda as indicator
from sdg_pipeline.sources.oecd import OecdResult, SdmxObservation
from sdg_pipeline.standardized import CURRENT_METHODOLOGY_VERIFIED


def observation(year: int, score: str, value: str) -> SdmxObservation:
    """Build one small parsed OECD observation."""

    return SdmxObservation(year, Decimal(value), {"SCORE": score})


def source_result(observations=()) -> OecdResult:
    """Build controlled provenance for standardized-output tests."""

    return OecdResult(
        observations=tuple(observations),
        source_organization=(
            "Organisation for Economic Co-operation and Development (OECD)"
        ),
        source_dataset="OECD Rio Markers fixture",
        source_url="https://example.invalid/oecd",
        retrieval_method="api",
        retrieval_date="2026-08-11",
        dataflow_id="DSD_RIOMRKR@DF_RIOMARKERS",
        dataflow_version="1.6",
    )


class BiodiversityOdaIndicatorTests(unittest.TestCase):
    """Protect the twin-indicator arithmetic, interpretation, and outputs."""

    def test_principal_plus_significant_uses_full_precision(self) -> None:
        result = indicator.calculate(
            [
                observation(2022, "1", "231.791554"),
                observation(2022, "2", "545.313317"),
            ],
            [2022],
        )

        self.assertEqual(Decimal("231.791554"), result.years[0].significant)
        self.assertEqual(Decimal("545.313317"), result.years[0].principal)
        self.assertEqual(Decimal("777.104871"), result.years[0].combined)

    def test_score_zero_is_excluded(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "excluded OECD score '0'"):
            indicator.calculate([observation(2022, "0", "999")], [2022])

    def test_absent_zero_aggregate_is_auditable(self) -> None:
        result = indicator.calculate(
            [observation(2006, "2", "59.344158")], [2006]
        )

        self.assertEqual(Decimal(0), result.years[0].significant)
        self.assertEqual(Decimal("59.344158"), result.years[0].combined)
        self.assertIn("treated as zero", result.warnings[0])

    def test_repeat_indicators_receive_identical_statistical_values(self) -> None:
        calculated = indicator.calculate(
            [observation(2022, "1", "1.25"), observation(2022, "2", "2.5")],
            [2022],
        ).years
        source = source_result()

        rows_a = indicator.build_standardized_observations(
            "15.a.1", calculated, source
        )
        rows_b = indicator.build_standardized_observations(
            "15.b.1", calculated, source
        )

        self.assertEqual(rows_a[0].value, rows_b[0].value)
        self.assertEqual(indicator.UNIT, rows_a[0].unit)
        self.assertEqual("United States", rows_a[0].geography)
        self.assertEqual({}, rows_a[0].disaggregation)
        self.assertEqual(
            "Organisation for Economic Co-operation and Development (OECD)",
            rows_a[0].source_organization,
        )
        self.assertEqual("OECD Rio Markers fixture", rows_a[0].source_dataset)
        self.assertEqual("https://example.invalid/oecd", rows_a[0].source_url)
        self.assertEqual("api", rows_a[0].retrieval_method)
        self.assertEqual("2026-08-11", rows_a[0].retrieval_date)
        self.assertEqual(
            CURRENT_METHODOLOGY_VERIFIED, rows_a[0].validation_status
        )

    def test_standardized_output_carries_methodology_warning(self) -> None:
        calculated = indicator.calculate(
            [observation(2022, "1", "1"), observation(2022, "2", "2")],
            [2022],
        ).years

        row = indicator.build_standardized_observations(
            "15.a.1", calculated, source_result()
        )[0]

        self.assertIn("component (a)", row.data_warning)
        self.assertIn("gross disbursements in current USD", row.data_warning)
        self.assertIn("intentionally does not reproduce", row.data_warning)
        self.assertEqual(indicator.METHODOLOGY_VARIANT, row.methodology_variant)

    def test_archive_comparison_is_diagnostic_not_a_match_requirement(self) -> None:
        calculated = [
            indicator.BiodiversityOdaYear(
                2022,
                principal=Decimal("500"),
                significant=Decimal("277.104871"),
                combined=Decimal("777.104871"),
            )
        ]

        comparison = indicator.compare_with_archive(
            calculated, {2022: Decimal("500.118799")}
        )

        self.assertEqual(1, len(comparison))
        self.assertEqual(Decimal("276.986072"), comparison[0].difference)
        self.assertIn("gross disbursements", comparison[0].archived_methodology)
        self.assertIn("commitments", comparison[0].current_methodology)


if __name__ == "__main__":
    unittest.main()
