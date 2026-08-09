"""Offline regression tests for the existing SDG 8.6.1 pipeline."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from tests.pipeline_test_utils import FIXTURE_ROOT, load_pipeline_module


PIPELINE = load_pipeline_module(
    "baseline_fetch_sdg_8_6_1", "fetch_sdg_8_6_1.py"
)


def load_baseline_fixture():
    """Return controlled BLS observations and their expected indicator values."""

    observations = {series_id: {} for series_id in PIPELINE.SERIES_IDS}
    expected = {}
    fixture_path = FIXTURE_ROOT / "sdg_8_6_1_baseline.csv"
    with fixture_path.open(encoding="utf-8", newline="") as fixture_file:
        for row in csv.DictReader(fixture_file):
            year = int(row["year"])
            observations[PIPELINE.ENROLLED_SERIES][year] = Decimal(row["enrolled"])
            observations[PIPELINE.NOT_ENROLLED_SERIES][year] = Decimal(
                row["not_enrolled"]
            )
            observations[PIPELINE.EMPLOYED_NOT_ENROLLED_SERIES][year] = Decimal(
                row["employed_not_enrolled"]
            )
            expected[year] = Decimal(row["expected_value"])
    return observations, expected


class Sdg861BaselineTests(unittest.TestCase):
    """Protect known values, calculation behavior, and output safety."""

    def test_representative_historical_values(self):
        observations, expected = load_baseline_fixture()
        rows = PIPELINE.calculate_rows(observations, "fixture")
        actual = {
            int(row["year"]): Decimal(str(row["calculated_value"])) for row in rows
        }

        self.assertEqual(set(expected), set(actual))
        for year, expected_value in expected.items():
            with self.subTest(year=year):
                self.assertEqual(expected_value, actual[year])

    def test_three_series_formula_and_rounding(self):
        observations = {
            PIPELINE.ENROLLED_SERIES: {2024: Decimal("40")},
            PIPELINE.NOT_ENROLLED_SERIES: {2024: Decimal("60")},
            PIPELINE.EMPLOYED_NOT_ENROLLED_SERIES: {2024: Decimal("30")},
        }

        row = PIPELINE.calculate_rows(observations, "fixture")[0]

        # 100 * (60 - 30) / (40 + 60) = 30.0
        self.assertEqual("30.0", row["calculated_value"])
        self.assertEqual("40", row["enrolled_population_thousands"])
        self.assertEqual("60", row["not_enrolled_population_thousands"])
        self.assertEqual("30", row["employed_not_enrolled_thousands"])

    def test_archive_validation_summary_for_fixture(self):
        observations, expected = load_baseline_fixture()
        rows = PIPELINE.calculate_rows(observations, "fixture")

        report = PIPELINE.validate_against_archive(rows, expected)

        self.assertEqual(6, report["overlapping_years"])
        self.assertEqual(6, report["exact_matches"])
        self.assertEqual(Decimal("0"), report["maximum_absolute_difference"])
        self.assertEqual([], report["mismatching_years"])

    def test_api_failure_uses_bulk_fallback(self):
        observations, _expected = load_baseline_fixture()
        with mock.patch.object(
            PIPELINE,
            "fetch_from_api",
            side_effect=PIPELINE.RetrievalError("simulated API outage"),
        ), mock.patch.object(
            PIPELINE, "fetch_from_bulk_data", return_value=observations
        ), mock.patch("sys.stderr", new_callable=io.StringIO):
            actual, method = PIPELINE.retrieve_bls_data()

        self.assertIs(observations, actual)
        self.assertEqual("bulk", method)

    def test_api_rejects_html_response(self):
        with mock.patch.object(
            PIPELINE, "year_chunks", return_value=[(2023, 2023)]
        ), mock.patch.object(
            PIPELINE,
            "request_bytes",
            return_value=(b"<html>maintenance</html>", "text/html"),
        ):
            with self.assertRaisesRegex(
                PIPELINE.RetrievalError, "non-JSON response"
            ):
                PIPELINE.fetch_from_api()

    def test_retrieval_failure_does_not_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "sdg_8_6_1.csv"
            output_path.write_text("previous successful output\n", encoding="utf-8")

            with mock.patch.object(
                PIPELINE, "OUTPUT_PATH", output_path
            ), mock.patch.object(
                PIPELINE,
                "retrieve_bls_data",
                side_effect=PIPELINE.RetrievalError("simulated total outage"),
            ), mock.patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as exit_context:
                    PIPELINE.main()

            self.assertEqual(1, exit_context.exception.code)
            self.assertEqual(
                "previous successful output\n",
                output_path.read_text(encoding="utf-8"),
            )

    def test_atomic_replace_failure_preserves_existing_output(self):
        observations, _expected = load_baseline_fixture()
        rows = PIPELINE.calculate_rows(observations, "fixture")
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "sdg_8_6_1.csv"
            output_path.write_text("previous successful output\n", encoding="utf-8")

            with mock.patch.object(
                PIPELINE, "OUTPUT_PATH", output_path
            ), mock.patch.object(
                PIPELINE.os, "replace", side_effect=OSError("simulated replace failure")
            ):
                with self.assertRaises(OSError):
                    PIPELINE.write_output_atomically(rows)

            self.assertEqual(
                "previous successful output\n",
                output_path.read_text(encoding="utf-8"),
            )
            self.assertEqual([], list(output_path.parent.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
