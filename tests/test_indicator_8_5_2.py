"""Focused offline tests for the SDG 8.5.2 indicator definition."""

from __future__ import annotations

import unittest
from decimal import Decimal

from tests.pipeline_test_utils import SRC_ROOT  # Makes src importable.
from sdg_pipeline.indicators import indicator_8_5_2 as indicator


def headline_observations(year: int = 2023):
    """Return one complete year of the four required published series."""

    return {
        "LNU04075630": {year: Decimal("7.2")},
        "LNU04075704": {year: Decimal("7.2")},
        "LNU04075409": {year: Decimal("3.6")},
        "LNU04075483": {year: Decimal("3.3")},
    }


def archive_fixture() -> indicator.ArchivedData:
    text = (
        "Year,Sex,Age,Disability Status,Units,Value\n"
        "2023,Men,16 +,Persons with a disability,Percent,7.2\n"
        "2023,Women,16 +,Persons with a disability,Percent,7.2\n"
        "2023,Men,16 +,Persons with no disability,Percent,3.6\n"
        "2023,Women,16 +,Persons with no disability,Percent,3.3\n"
        "2023,Men,20 - 24,Persons with a disability,Percent,11.0\n"
    )
    return indicator.parse_archived_values(text)


class Indicator852Tests(unittest.TestCase):
    """Protect mappings, validation, archive comparison, and row identity."""

    def test_headline_series_map_to_expected_sex_and_disability_labels(self):
        mappings = {
            item.series_id: dict(item.disaggregation)
            for item in indicator.HEADLINE_SERIES
        }

        self.assertEqual(
            {"sex": "Male", "disability": "No disability"},
            mappings["LNU04075409"],
        )
        self.assertEqual(
            {"sex": "Female", "disability": "No disability"},
            mappings["LNU04075483"],
        )
        self.assertEqual(
            {"sex": "Male", "disability": "With disability"},
            mappings["LNU04075630"],
        )
        self.assertEqual(
            {"sex": "Female", "disability": "With disability"},
            mappings["LNU04075704"],
        )

    def test_age_series_use_age_and_disability_but_do_not_invent_sex(self):
        definitions = {
            item.series_id: dict(item.disaggregation)
            for item in indicator.AGE_SERIES
        }

        self.assertEqual(
            {"age": "16-19", "disability": "No disability"},
            definitions["LNU04074596"],
        )
        self.assertEqual(
            {"age": "16-64", "disability": "With disability"},
            definitions["LNU04076950"],
        )
        self.assertTrue(all("sex" not in value for value in definitions.values()))

    def test_only_a01_period_is_accepted(self):
        self.assertEqual((2023,), indicator.validate_observations(headline_observations()))
        with self.assertRaisesRegex(RuntimeError, "requires BLS A01"):
            indicator.validate_observations(headline_observations(), "M13")

    def test_missing_optional_age_series_is_not_a_failure(self):
        observations = headline_observations()

        years = indicator.validate_observations(observations)

        self.assertEqual((2023,), years)
        self.assertEqual(
            set(indicator.OPTIONAL_AGE_SERIES_IDS),
            set(indicator.missing_optional_series(observations)),
        )

    def test_archive_comparison_tests_only_directly_comparable_rows(self):
        report = indicator.validate_against_archive(
            headline_observations(), archive_fixture()
        )

        self.assertEqual(4, report["overlapping_rows"])
        self.assertEqual(4, report["exact_matches"])
        self.assertEqual(Decimal("0.0"), report["maximum_absolute_difference"])
        self.assertEqual([], report["mismatching_rows"])
        self.assertEqual(1, report["non_comparable_archived_rows"])

    def test_standardized_rows_are_unique_and_preserve_published_values(self):
        observations = headline_observations()
        observations["LNU04074596"] = {2023: Decimal("12.4")}
        standardized = indicator.build_standardized_observations(
            observations,
            archive_fixture(),
            "U.S. Bureau of Labor Statistics",
            "https://example.invalid/bls",
            "api",
            "2026-08-13",
        )

        identities = {
            (item.year, tuple(sorted(item.disaggregation.items())))
            for item in standardized
        }
        self.assertEqual(5, len(standardized))
        self.assertEqual(len(standardized), len(identities))
        self.assertEqual(
            {"7.2", "3.6", "3.3", "12.4"},
            {str(item.value) for item in standardized},
        )
        self.assertTrue(
            all(item.source_dataset == indicator.SOURCE_DATASET for item in standardized)
        )

    def test_2025_row_carries_shutdown_and_age_coverage_warnings(self):
        warning = indicator.data_warning(2025)

        self.assertIn("begins at age 16", warning)
        self.assertIn("11-month averages", warning)
        self.assertEqual(indicator.AGE_COVERAGE_WARNING, indicator.data_warning(2024))


if __name__ == "__main__":
    unittest.main()
