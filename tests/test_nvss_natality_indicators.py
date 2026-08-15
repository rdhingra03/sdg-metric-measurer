"""Offline statistical tests for SDG 3.1.2 and SDG 3.7.2."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

from tests.pipeline_test_utils import SRC_ROOT
from sdg_pipeline.indicators import indicator_3_1_2 as skilled
from sdg_pipeline.indicators import indicator_3_7_2 as adolescent
from sdg_pipeline.sources import nvss_natality as nvss
from sdg_pipeline.standardized import ARCHIVE_MATCHED, observation_to_row


def attendant_rows():
    births = {"1": 50, "2": 10, "3": 20, "4": 10, "5": 5, "9": 5}
    return tuple(
        nvss.NatalityObservation(
            year=2024,
            category_code=code,
            category_label=label,
            births=births[code],
            female_population=None,
            source_reported_fertility_rate=None,
            suppression_status="not_suppressed",
            source_notes=("fixture",),
        )
        for code, label in nvss.MEDICAL_ATTENDANT_LABELS.items()
    )


def age_rows():
    return (
        nvss.NatalityObservation(
            2024, "15", "Under 15 years", 2_000, 10_000_000,
            Decimal("0.2"), "not_suppressed", ("fixture",)
        ),
        nvss.NatalityObservation(
            2024, "15-19", "15-19 years", 130_000, 10_000_000,
            # Deliberately wrong: the indicator must use counts and the female
            # denominator, not blindly copy this displayed value.
            Decimal("99.9"), "not_suppressed", ("fixture",)
        ),
    )


def source(observations):
    return nvss.NvssNatalityResult(
        observations=observations,
        source_organization=nvss.SOURCE_ORGANIZATION,
        source_dataset=nvss.SOURCE_DATASET,
        source_url=nvss.WONDER_QUERY_URL,
        retrieval_method="cdc_wonder_api",
        retrieval_date="2026-08-13",
    )


class SkilledAttendanceTests(unittest.TestCase):
    def test_classification_numerator_denominator_and_percentage(self):
        value = skilled.calculate(attendant_rows())[0]

        self.assertEqual(frozenset({"1", "2", "3", "4"}), skilled.SKILLED_ATTENDANT_CODES)
        self.assertEqual(90, value.skilled_births)
        self.assertEqual(100, value.total_births)
        self.assertEqual(Decimal("90"), value.percentage)
        self.assertEqual(4, len(value.included_categories))

    def test_missing_category_and_suppression_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            skilled.calculate(attendant_rows()[:-1])
        suppressed = list(attendant_rows())
        suppressed[0] = nvss.NatalityObservation(
            2024, "1", "MD", None, None, None, "suppressed", ("fixture",)
        )
        with self.assertRaisesRegex(RuntimeError, "suppressed"):
            skilled.calculate(suppressed)

    def test_archive_precision_and_standardized_schema(self):
        value = skilled.calculate(attendant_rows())[0]
        standardized = skilled.build_standardized(
            [value], source({"3.1.2": attendant_rows()}), {2024: Decimal("90.0")}
        )[0]
        row = observation_to_row(standardized)

        self.assertEqual(ARCHIVE_MATCHED, standardized.validation_status)
        self.assertEqual("{}", row["disaggregation"])
        self.assertEqual(15, len(row))


class AdolescentBirthRateTests(unittest.TestCase):
    def test_two_age_groups_use_births_and_female_denominators(self):
        values = adolescent.calculate(age_rows())
        by_age = {value.age_group: value for value in values}

        self.assertEqual({"10-14", "15-19"}, set(by_age))
        self.assertEqual(Decimal("0.2"), by_age["10-14"].rate)
        self.assertEqual(Decimal("13"), by_age["15-19"].rate)
        self.assertNotEqual(
            by_age["15-19"].source_reported_rate, by_age["15-19"].rate
        )

    def test_total_or_invalid_population_cannot_be_substituted(self):
        invalid = list(age_rows())
        invalid[0] = nvss.NatalityObservation(
            2024, "15", "Under 15 years", 2_000, 0,
            Decimal("0.2"), "not_suppressed", ("fixture",)
        )
        with self.assertRaisesRegex(RuntimeError, "female population"):
            adolescent.calculate(invalid)

    def test_archive_precision_disaggregation_and_uniqueness(self):
        values = adolescent.calculate(age_rows())
        standardized = adolescent.build_standardized(
            values,
            source({"3.7.2": age_rows()}),
            {(2024, "10-14"): Decimal("0.2"), (2024, "15-19"): Decimal("13.0")},
        )
        rows = [observation_to_row(item) for item in standardized]

        self.assertEqual([ARCHIVE_MATCHED, ARCHIVE_MATCHED], [item.validation_status for item in standardized])
        self.assertEqual(
            [{"age": "10-14"}, {"age": "15-19"}],
            [json.loads(row["disaggregation"]) for row in rows],
        )
        self.assertEqual(2, len({(row["year"], row["disaggregation"]) for row in rows}))


if __name__ == "__main__":
    unittest.main()
