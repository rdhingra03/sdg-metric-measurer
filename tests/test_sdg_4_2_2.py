"""Offline regression tests for the existing SDG 4.2.2 pipeline."""

from __future__ import annotations

import argparse
import csv
import io
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest import mock

from tests.pipeline_test_utils import FIXTURE_ROOT, load_pipeline_module


PIPELINE = load_pipeline_module(
    "baseline_fetch_sdg_4_2_2", "fetch_sdg_4_2_2.py"
)


def load_baseline_fixture():
    """Return aggregate CPS groups and archive values from a small fixture."""

    groups = {}
    archived = {}
    fixture_path = FIXTURE_ROOT / "sdg_4_2_2_baseline.csv"
    with fixture_path.open(encoding="utf-8", newline="") as fixture_file:
        for row in csv.DictReader(fixture_file):
            year = int(row["year"])
            group_name = row["group"]
            numerator = int(row["raw_weighted_numerator"])
            denominator = int(row["raw_weighted_denominator"])
            groups[(year, group_name)] = PIPELINE.GroupResult(
                raw_weighted_numerator=numerator,
                raw_weighted_denominator=denominator,
                unweighted_numerator=int(row["unweighted_numerator"]),
                unweighted_denominator=int(row["unweighted_denominator"]),
                calculated_fraction=Fraction(100 * numerator, denominator),
            )
            expected_text = row["expected_value"]
            archived[(year, group_name)] = PIPELINE.ArchivedValue(
                value=Decimal(expected_text),
                decimal_places=len(expected_text.partition(".")[2]),
            )
    return groups, archived


def build_year_results(groups):
    """Create YearResult objects needed by the current validation function."""

    empty_group = PIPELINE.GroupResult(0, 1, 0, 1, Fraction(0, 1))
    results = []
    for year in range(2018, 2023):
        results.append(
            PIPELINE.YearResult(
                year=year,
                weight_variable="PWSUPWGT",
                source_url="https://example.invalid/fixture",
                retrieval_method="fixture",
                national=groups[(year, "National")],
                male=groups.get((year, "Male"), empty_group),
                female=groups.get((year, "Female"), empty_group),
            )
        )
    return results


class Sdg422BaselineTests(unittest.TestCase):
    """Protect known values, weighted logic, fallback, and output safety."""

    def test_representative_archived_national_values(self):
        groups, archived = load_baseline_fixture()
        for year in range(2018, 2023):
            with self.subTest(year=year):
                difference = PIPELINE.compare_at_archived_precision(
                    groups[(year, "National")].calculated_fraction,
                    archived[(year, "National")],
                )
                self.assertEqual(Decimal("0"), difference)

    def test_2022_archived_sex_values(self):
        groups, archived = load_baseline_fixture()
        for sex in ("Male", "Female"):
            with self.subTest(sex=sex):
                difference = PIPELINE.compare_at_archived_precision(
                    groups[(2022, sex)].calculated_fraction,
                    archived[(2022, sex)],
                )
                self.assertEqual(Decimal("0"), difference)

    def test_weighted_numerator_denominator_and_sex_logic(self):
        records = [
            PIPELINE.PersonRecord(5, 1, 1, 1, 30_000),
            PIPELINE.PersonRecord(5, 2, -1, 1, 10_000),
            PIPELINE.PersonRecord(5, 1, 4, 2, 20_000),
            PIPELINE.PersonRecord(5, 2, -1, 2, 20_000),
            # A nonpositive weight is not part of the valid denominator.
            PIPELINE.PersonRecord(5, 1, 3, 2, 0),
        ]

        result = PIPELINE.calculate_year(
            2022, records, "fixture", "https://example.invalid/fixture"
        )

        self.assertEqual(50_000, result.national.raw_weighted_numerator)
        self.assertEqual(80_000, result.national.raw_weighted_denominator)
        self.assertEqual(2, result.national.unweighted_numerator)
        self.assertEqual(4, result.national.unweighted_denominator)
        self.assertEqual(Fraction(125, 2), result.national.calculated_fraction)
        self.assertEqual(Fraction(75, 1), result.male.calculated_fraction)
        self.assertEqual(Fraction(50, 1), result.female.calculated_fraction)

    def test_enrolled_record_with_invalid_grade_is_rejected(self):
        records = [PIPELINE.PersonRecord(5, 1, 17, 1, 10_000)]

        with self.assertRaisesRegex(RuntimeError, "organized-learning grade range"):
            PIPELINE.calculate_year(
                2022, records, "fixture", "https://example.invalid/fixture"
            )

    def test_archive_validation_summary_for_fixture(self):
        groups, archived = load_baseline_fixture()
        results = build_year_results(groups)

        report = PIPELINE.validate_results(results, archived)

        self.assertEqual(5, report["national_overlaps"])
        self.assertEqual(5, report["national_exact_matches"])
        self.assertEqual(Decimal("0"), report["national_maximum_difference"])
        self.assertEqual([], report["national_mismatches"])
        self.assertEqual(2, report["sex_overlaps"])
        self.assertEqual(2, report["sex_exact_matches"])
        self.assertEqual(Decimal("0"), report["sex_maximum_difference"])
        self.assertEqual([], report["sex_mismatches"])

    def test_api_failure_uses_download_fallback(self):
        records = [
            PIPELINE.PersonRecord(5, 1, 3, 1, 10_000),
            PIPELINE.PersonRecord(5, 2, -1, 2, 10_000),
        ]
        fallback_url = "https://example.invalid/census-fixture.zip"
        with mock.patch.object(
            PIPELINE,
            "fetch_from_api",
            side_effect=PIPELINE.RetrievalError("simulated API outage"),
        ), mock.patch.object(
            PIPELINE, "fetch_from_download", return_value=(records, fallback_url)
        ), mock.patch("sys.stderr", new_callable=io.StringIO):
            actual_records, method, source_url = PIPELINE.retrieve_year(
                2022, "fixture-api-key"
            )

        self.assertIs(records, actual_records)
        self.assertEqual("download", method)
        self.assertEqual(fallback_url, source_url)

    def test_historical_weight_transition(self):
        self.assertEqual("PWSSWGT", PIPELINE.weight_variable_for_year(2005))
        self.assertEqual("PWSUPWGT", PIPELINE.weight_variable_for_year(2006))

    def test_retrieval_failure_does_not_overwrite_existing_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            national_path = output_directory / "sdg_4_2_2.csv"
            sex_path = output_directory / "sdg_4_2_2_by_sex.csv"
            national_path.write_text("previous national output\n", encoding="utf-8")
            sex_path.write_text("previous sex output\n", encoding="utf-8")

            arguments = argparse.Namespace(start_year=2018, end_year=2018)
            with mock.patch.object(
                PIPELINE, "NATIONAL_OUTPUT_PATH", national_path
            ), mock.patch.object(
                PIPELINE, "SEX_OUTPUT_PATH", sex_path
            ), mock.patch.object(
                PIPELINE, "parse_arguments", return_value=arguments
            ), mock.patch.object(
                PIPELINE,
                "retrieve_year",
                side_effect=PIPELINE.RetrievalError("simulated total outage"),
            ), mock.patch.dict(PIPELINE.os.environ, {}, clear=True), mock.patch(
                "sys.stderr", new_callable=io.StringIO
            ):
                with self.assertRaises(SystemExit) as exit_context:
                    PIPELINE.main()

            self.assertEqual(1, exit_context.exception.code)
            self.assertEqual(
                "previous national output\n",
                national_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "previous sex output\n", sex_path.read_text(encoding="utf-8")
            )

    def test_first_atomic_replace_failure_preserves_both_outputs(self):
        groups, _archived = load_baseline_fixture()
        results = build_year_results(groups)
        national_rows, sex_rows = PIPELINE.build_output_rows(
            results, retrieval_date="2026-08-09"
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            national_path = output_directory / "sdg_4_2_2.csv"
            sex_path = output_directory / "sdg_4_2_2_by_sex.csv"
            national_path.write_text("previous national output\n", encoding="utf-8")
            sex_path.write_text("previous sex output\n", encoding="utf-8")

            with mock.patch.object(
                PIPELINE, "NATIONAL_OUTPUT_PATH", national_path
            ), mock.patch.object(
                PIPELINE, "SEX_OUTPUT_PATH", sex_path
            ), mock.patch.object(
                PIPELINE.os, "replace", side_effect=OSError("simulated replace failure")
            ):
                with self.assertRaises(OSError):
                    PIPELINE.write_outputs_atomically(national_rows, sex_rows)

            self.assertEqual(
                "previous national output\n",
                national_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "previous sex output\n", sex_path.read_text(encoding="utf-8")
            )
            self.assertEqual([], list(output_directory.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
