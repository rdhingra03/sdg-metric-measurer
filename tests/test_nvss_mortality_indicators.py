"""Offline tests for the three current-methodology mortality indicators."""

from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from tests.pipeline_test_utils import SRC_ROOT  # Makes src importable.
from sdg_pipeline.indicators import indicator_3_4_2 as suicide
from sdg_pipeline.indicators import indicator_3_6_1 as traffic
from sdg_pipeline.indicators import indicator_3_9_3 as poisoning
from sdg_pipeline.sources import nvss_mortality as nvss
from sdg_pipeline.standardized import CURRENT_METHODOLOGY_VERIFIED


def observation(icd10_selection) -> nvss.MortalityObservation:
    return nvss.MortalityObservation(
        year=2024,
        deaths=200,
        population=2_000_000,
        crude_rate=Decimal("10"),
        # An intentionally different reported value proves calculations do not
        # substitute an age-adjusted or source-display rate for exact counts.
        source_reported_crude_rate=Decimal("99.9"),
        icd10_selection=icd10_selection,
        disaggregation={},
        suppression_status="not_suppressed",
        source_notes=("fixture",),
    )


def source(rows, key) -> nvss.NvssMortalityResult:
    return nvss.NvssMortalityResult(
        observations={key: tuple(rows)},
        source_organization=nvss.SOURCE_ORGANIZATION,
        source_dataset=nvss.SOURCE_DATASET,
        source_url=nvss.WONDER_QUERY_URL,
        retrieval_method="cdc_wonder_api",
        retrieval_date="2026-08-13",
    )


class NvssMortalityIndicatorTests(unittest.TestCase):
    def test_suicide_selection_and_crude_calculation(self) -> None:
        row = observation(suicide.ICD10_SELECTION)

        self.assertEqual(("X60-X84", "Y87.0"), suicide.ICD10_SELECTION)
        self.assertEqual(Decimal("10"), suicide.calculate(row))
        self.assertNotEqual(row.source_reported_crude_rate, suicide.calculate(row))

    def test_suicide_rejects_wrong_selection_and_suppression(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "wrong ICD"):
            suicide.calculate(observation(("X60-X84",)))
        with self.assertRaisesRegex(RuntimeError, "suppressed"):
            suicide.calculate(
                replace(
                    observation(suicide.ICD10_SELECTION),
                    deaths=None,
                    population=None,
                    suppression_status="suppressed",
                )
            )

    def test_traffic_uses_current_exact_selection_and_crude_rate(self) -> None:
        row = observation(traffic.ICD10_SELECTION)

        self.assertEqual(378, len(traffic.ICD10_SELECTION))
        self.assertIn("V01.1", traffic.ICD10_SELECTION)
        self.assertIn("V01.9", traffic.ICD10_SELECTION)
        self.assertNotIn("V01.2", traffic.ICD10_SELECTION)
        self.assertIn("Y85.0", traffic.ICD10_SELECTION)
        self.assertEqual("X59.4", traffic.UN_CODE_UNAVAILABLE_IN_WONDER)
        self.assertNotIn("X59.4", traffic.ICD10_SELECTION)
        self.assertEqual(Decimal("10"), traffic.calculate(row))

    def test_poisoning_selection_excludes_archive_only_codes(self) -> None:
        row = observation(poisoning.ICD10_SELECTION)

        self.assertEqual(
            ("X40", "X43", "X46", "X47", "X48", "X49"),
            poisoning.ICD10_SELECTION,
        )
        for code in poisoning.EXCLUDED_ARCHIVE_ONLY_CODES:
            self.assertNotIn(code, poisoning.ICD10_SELECTION)
        self.assertEqual(Decimal("10"), poisoning.calculate(row))

    def test_all_modules_build_current_methodology_standardized_rows(self) -> None:
        for module in (suicide, traffic, poisoning):
            row = observation(module.ICD10_SELECTION)
            result = source([row], module.INDICATOR_ID)

            standardized = module.build_standardized([row], result)[0]

            self.assertEqual(module.INDICATOR_ID, standardized.indicator_id)
            self.assertEqual("10.000000", standardized.value)
            self.assertEqual(
                CURRENT_METHODOLOGY_VERIFIED, standardized.validation_status
            )
            self.assertIn("crude", standardized.data_warning.lower())
            self.assertIn("age-adjusted", standardized.data_warning.lower())


if __name__ == "__main__":
    unittest.main()
