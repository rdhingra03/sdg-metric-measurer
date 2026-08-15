"""Offline orchestration and card tests for the natality batch."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from tests.pipeline_test_utils import load_pipeline_module
from sdg_pipeline.output import write_csv_outputs_atomically


pipeline = load_pipeline_module(
    "fetch_nvss_natality_batch", "fetch_nvss_natality_batch.py"
)
cards = load_pipeline_module("build_data_cards_for_natality", "build_data_cards.py")


def card_observation(indicator_id, year, value, disaggregation):
    row = {
        "indicator_id": indicator_id,
        "indicator_title": "Fixture",
        "year": str(year),
        "value": value,
        "unit": "percent" if indicator_id == "3.1.2" else "births per 1,000 women",
        "geography": "United States",
        "disaggregation": "{}",
        "source_organization": "NCHS",
        "source_url": "https://wonder.cdc.gov/test",
        "validation_status": "archive_matched",
        "data_warning": "",
    }
    return cards.Observation(row, year, Decimal(value), disaggregation)


class NatalityBatchTests(unittest.TestCase):
    def test_queries_are_one_shared_natality_batch(self):
        queries = pipeline.build_queries()

        self.assertEqual(["3.1.2", "3.7.2"], [query.key for query in queries])
        self.assertFalse(queries[0].include_fertility_rate)
        self.assertTrue(queries[1].include_fertility_rate)

    def test_first_output_failure_preserves_every_previous_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = [Path(temporary_directory) / f"output-{number}.csv" for number in range(4)]
            for path in paths:
                path.write_text("previous successful output\n", encoding="utf-8")
            outputs = [(path, ["value"], [{"value": "new"}]) for path in paths]
            with mock.patch(
                "sdg_pipeline.output.os.replace", side_effect=OSError("simulated failure")
            ):
                with self.assertRaises(OSError):
                    write_csv_outputs_atomically(outputs)
            self.assertTrue(
                all(path.read_text(encoding="utf-8") == "previous successful output\n" for path in paths)
            )

    def test_card_builder_contains_each_new_indicator_once(self):
        self.assertEqual(1, cards.INDICATOR_ORDER.count("3.1.2"))
        self.assertEqual(1, cards.INDICATOR_ORDER.count("3.7.2"))
        self.assertEqual(13, len(cards.INDICATOR_ORDER))

    def test_3_7_2_card_uses_latest_15_19_headline_and_10_14_secondary(self):
        observations = [
            card_observation("3.7.2", 2023, "14", {"age": "15-19"}),
            card_observation("3.7.2", 2024, "13", {"age": "15-19"}),
            card_observation("3.7.2", 2024, "0.2", {"age": "10-14"}),
        ]

        metrics = cards.select_metrics("3.7.2", observations)

        self.assertEqual(["Ages 15-19", "Ages 10-14"], [item.label for item in metrics])
        self.assertEqual([Decimal("13"), Decimal("0.2")], [item.observation.value for item in metrics])

    def test_3_1_2_card_uses_latest_national_value(self):
        observations = [
            card_observation("3.1.2", 2023, "98.7", {}),
            card_observation("3.1.2", 2024, "98.8", {}),
        ]
        metrics = cards.select_metrics("3.1.2", observations)
        self.assertEqual(1, len(metrics))
        self.assertEqual(2024, metrics[0].observation.year)


if __name__ == "__main__":
    unittest.main()
