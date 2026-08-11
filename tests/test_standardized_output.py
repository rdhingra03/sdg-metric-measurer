"""Tests for the Phase 5 standardized processed-data outputs."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from tests.pipeline_test_utils import SRC_ROOT  # Makes the src package importable.
from sdg_pipeline.indicators import indicator_4_2_2 as indicator_422
from sdg_pipeline.indicators import indicator_8_6_1 as indicator_861
from sdg_pipeline.standardized import (
    ARCHIVE_MATCHED,
    NOT_ARCHIVE_VALIDATED,
    STANDARDIZED_COLUMNS,
    StandardizedObservation,
    observation_to_row,
    write_standardized_csv,
)


def standard_observation(**changes) -> StandardizedObservation:
    """Build a complete, small observation for writer tests."""

    values = {
        "indicator_id": "1.2.3",
        "indicator_title": "Fixture indicator",
        "year": 2022,
        "value": "12.5",
        "unit": "percent",
        "geography": "United States",
        "disaggregation": {},
        "source_organization": "Official agency",
        "source_dataset": "Official dataset",
        "source_url": "https://example.invalid/data",
        "retrieval_method": "fixture",
        "retrieval_date": "2026-08-11",
        "methodology_variant": "fixture_method",
        "validation_status": ARCHIVE_MATCHED,
        "data_warning": "",
    }
    values.update(changes)
    return StandardizedObservation(**values)


class StandardizedWriterTests(unittest.TestCase):
    """Protect the common schema, JSON format, and failure-safe write."""

    def test_csv_has_exact_required_column_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "standardized.csv"
            write_standardized_csv(output_path, [standard_observation()])

            with output_path.open(encoding="utf-8", newline="") as input_file:
                reader = csv.DictReader(input_file)
                row = next(reader)

            self.assertEqual(STANDARDIZED_COLUMNS, reader.fieldnames)
            self.assertEqual(set(STANDARDIZED_COLUMNS), set(row))

    def test_disaggregation_is_stable_compact_json(self) -> None:
        national = observation_to_row(standard_observation())
        sex = observation_to_row(
            standard_observation(disaggregation={"sex": "Male"})
        )

        self.assertEqual("{}", national["disaggregation"])
        self.assertEqual('{"sex":"Male"}', sex["disaggregation"])
        self.assertEqual({"sex": "Male"}, json.loads(sex["disaggregation"]))

    def test_missing_required_fields_are_rejected(self) -> None:
        incomplete = {"indicator_id": "1.2.3"}

        with self.assertRaisesRegex(ValueError, "missing columns"):
            observation_to_row(incomplete)

    def test_failed_atomic_replace_preserves_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "standardized.csv"
            output_path.write_text("previous successful output\n", encoding="utf-8")

            with mock.patch(
                "sdg_pipeline.output.os.replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaises(OSError):
                    write_standardized_csv(output_path, [standard_observation()])

            self.assertEqual(
                "previous successful output\n",
                output_path.read_text(encoding="utf-8"),
            )
            self.assertEqual([], list(output_path.parent.glob(".*.tmp")))


class StandardizedIndicatorTests(unittest.TestCase):
    """Protect equivalence with each indicator's established output values."""

    def test_8_6_1_values_and_per_year_validation_status(self) -> None:
        source = {
            indicator_861.ENROLLED_SERIES: {
                2022: Decimal("40"),
                2023: Decimal("50"),
            },
            indicator_861.NOT_ENROLLED_SERIES: {
                2022: Decimal("60"),
                2023: Decimal("50"),
            },
            indicator_861.EMPLOYED_NOT_ENROLLED_SERIES: {
                2022: Decimal("30"),
                2023: Decimal("40"),
            },
        }
        legacy_rows = indicator_861.calculate(source, "api", "2026-08-11")
        standardized = indicator_861.build_standardized_observations(
            legacy_rows,
            {2022: Decimal("30.0")},
            "BLS",
            "LABSTAT",
            "https://example.invalid/bls",
        )

        self.assertEqual(
            [row["calculated_value"] for row in legacy_rows],
            [observation.value for observation in standardized],
        )
        self.assertEqual(ARCHIVE_MATCHED, standardized[0].validation_status)
        self.assertEqual(NOT_ARCHIVE_VALIDATED, standardized[1].validation_status)
        self.assertEqual({}, standardized[0].disaggregation)

    def test_4_2_2_national_and_sex_values_and_statuses(self) -> None:
        records = [
            indicator_422.PersonRecord(5, 1, 1, 1, 30_000),
            indicator_422.PersonRecord(5, 2, -1, 1, 10_000),
            indicator_422.PersonRecord(5, 1, 4, 2, 20_000),
            indicator_422.PersonRecord(5, 2, -1, 2, 20_000),
        ]
        result_2022 = indicator_422.calculate(
            2022, records, "download", "https://example.invalid/2022", "PWSUPWGT"
        )
        result_2023 = indicator_422.calculate(
            2023, records, "download", "https://example.invalid/2023", "PWSUPWGT"
        )
        archived = {}
        for name, group in (
            ("National", result_2022.national),
            ("Male", result_2022.male),
            ("Female", result_2022.female),
        ):
            text = indicator_422.decimal_text(
                indicator_422.fraction_to_decimal(group.calculated_fraction)
            )
            archived[(2022, name)] = indicator_422.ArchivedValue(
                Decimal(text), len(text.partition(".")[2])
            )

        national_rows, sex_rows = indicator_422.build_output_rows(
            [result_2022, result_2023], "2026-08-11", 10_000
        )
        standardized = indicator_422.build_standardized_observations(
            [result_2022, result_2023],
            archived,
            "2026-08-11",
            "Census / NCES",
            "CPS School Enrollment",
        )

        expected_values = [
            national_rows[0]["calculated_value"],
            sex_rows[0]["calculated_value"],
            sex_rows[1]["calculated_value"],
            national_rows[1]["calculated_value"],
            sex_rows[2]["calculated_value"],
            sex_rows[3]["calculated_value"],
        ]
        self.assertEqual(expected_values, [item.value for item in standardized])
        self.assertEqual(
            [ARCHIVE_MATCHED] * 3 + [NOT_ARCHIVE_VALIDATED] * 3,
            [item.validation_status for item in standardized],
        )
        self.assertEqual(
            [{}, {"sex": "Male"}, {"sex": "Female"}],
            [item.disaggregation for item in standardized[:3]],
        )


if __name__ == "__main__":
    unittest.main()
