"""Direct tests for the SDG 8.6.1 statistical definition."""

from __future__ import annotations

import unittest
from decimal import Decimal

from tests.pipeline_test_utils import SRC_ROOT  # Makes the src package importable.
from sdg_pipeline.indicators import indicator_8_6_1 as indicator


class Indicator861Tests(unittest.TestCase):
    """Protect formula, validation, rounding, and archive comparison rules."""

    def test_formula_preserves_full_precision_and_rounds_for_presentation(self) -> None:
        calculation = indicator.calculate_value(
            enrolled=Decimal("1"),
            not_enrolled=Decimal("2"),
            employed_not_enrolled=Decimal("1"),
            year=2022,
        )

        self.assertEqual(Decimal("100") / Decimal("3"), calculation.unrounded_value)
        self.assertEqual(Decimal("33.3"), calculation.presented_value)

    def test_input_validation_requires_all_series_and_matching_years(self) -> None:
        missing = {
            indicator.ENROLLED_SERIES: {2022: Decimal("1")},
            indicator.NOT_ENROLLED_SERIES: {2022: Decimal("2")},
        }
        with self.assertRaisesRegex(RuntimeError, "missing required BLS series"):
            indicator.validate_observations(missing)

        mismatched = {
            indicator.ENROLLED_SERIES: {2022: Decimal("1")},
            indicator.NOT_ENROLLED_SERIES: {2022: Decimal("2")},
            indicator.EMPLOYED_NOT_ENROLLED_SERIES: {2021: Decimal("1")},
        }
        with self.assertRaisesRegex(RuntimeError, "matching years"):
            indicator.validate_observations(mismatched)

    def test_indicator_value_validation_rejects_impossible_inputs(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "non-positive"):
            indicator.calculate_value(
                Decimal("0"), Decimal("0"), Decimal("0"), 2022
            )
        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            indicator.calculate_value(
                Decimal("10"), Decimal("5"), Decimal("6"), 2022
            )

    def test_archive_parsing_and_comparison_use_one_decimal(self) -> None:
        archived = indicator.parse_archived_values("Year,Value\n2022,17.24\n")
        report = indicator.validate_against_archive(
            [{"year": 2022, "calculated_value": "17.2"}], archived
        )

        self.assertEqual(Decimal("17.2"), archived[2022])
        self.assertEqual(1, report["exact_matches"])
        self.assertEqual(Decimal("0.0"), report["maximum_absolute_difference"])


if __name__ == "__main__":
    unittest.main()

