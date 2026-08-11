"""Direct tests for the SDG 4.2.2 statistical definition."""

from __future__ import annotations

import unittest
from decimal import Decimal
from fractions import Fraction

from tests.pipeline_test_utils import SRC_ROOT  # Makes the src package importable.
from sdg_pipeline.indicators import indicator_4_2_2 as indicator
from sdg_pipeline.sources.census_cps import CpsObservation


def observation(age: int, enrollment: int, grade: int, sex: int, weight: int):
    """Build one small named CPS observation for indicator tests."""

    return CpsObservation(
        {
            "PRTAGE": age,
            "PESCH35": enrollment,
            "PECHGRDE": grade,
            "PESEX": sex,
            "PWSUPWGT": weight,
        }
    )


class Indicator422Tests(unittest.TestCase):
    """Protect age, enrollment, grade, weighting, sex, and precision rules."""

    def test_exact_age_five_selection(self) -> None:
        records = indicator.select_age_five(
            [
                observation(5, 1, 3, 1, 10_000),
                observation(6, 1, 4, 2, 20_000),
            ],
            "PWSUPWGT",
        )

        self.assertEqual(1, len(records))
        self.assertEqual(5, records[0].age)

    def test_enrollment_positive_weights_national_and_sex_calculation(self) -> None:
        records = [
            indicator.PersonRecord(5, 1, 1, 1, 30_000),
            indicator.PersonRecord(5, 2, -1, 1, 10_000),
            indicator.PersonRecord(5, 1, 4, 2, 20_000),
            indicator.PersonRecord(5, 2, -1, 2, 20_000),
            indicator.PersonRecord(5, 1, 3, 2, 0),
        ]

        result = indicator.calculate(
            2022, records, "fixture", "https://example.invalid", "PWSUPWGT"
        )

        self.assertTrue(indicator.is_enrolled(records[0]))
        self.assertFalse(indicator.is_enrolled(records[1]))
        self.assertEqual(50_000, result.national.raw_weighted_numerator)
        self.assertEqual(80_000, result.national.raw_weighted_denominator)
        self.assertEqual(4, result.national.unweighted_denominator)
        self.assertEqual(Fraction(75, 1), result.male.calculated_fraction)
        self.assertEqual(Fraction(50, 1), result.female.calculated_fraction)

    def test_enrolled_grade_consistency_validation(self) -> None:
        records = [indicator.PersonRecord(5, 1, 17, 1, 10_000)]

        with self.assertRaisesRegex(RuntimeError, "organized-learning grade range"):
            indicator.validate_records(records, 2022)

    def test_archive_comparison_uses_stored_precision(self) -> None:
        archived = indicator.ArchivedValue(Decimal("66.7"), decimal_places=1)

        difference = indicator.compare_at_archived_precision(
            Fraction(200, 3), archived
        )

        self.assertEqual(Decimal("0.0"), difference)


if __name__ == "__main__":
    unittest.main()
