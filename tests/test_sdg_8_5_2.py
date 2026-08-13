"""Offline orchestration tests for the SDG 8.5.2 pipeline and data card."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from tests.pipeline_test_utils import load_pipeline_module
from sdg_pipeline.sources.bls import BlsResult


pipeline = load_pipeline_module("fetch_sdg_8_5_2", "fetch_sdg_8_5_2.py")
cards = load_pipeline_module("build_data_cards_for_8_5_2", "build_data_cards.py")


class Pipeline852Tests(unittest.TestCase):
    """Protect use of the existing connector and required annual period."""

    def test_pipeline_requests_verified_series_with_a01(self):
        sentinel = BlsResult(
            observations={},
            source_organization="BLS",
            source_dataset="LABSTAT",
            source_url="https://example.invalid/bls",
            retrieval_method="api",
            retrieval_date="2026-08-13",
        )
        with mock.patch.object(pipeline.bls, "retrieve", return_value=sentinel) as retrieve:
            result = pipeline.retrieve_bls_data()

        self.assertIs(sentinel, result)
        self.assertEqual(pipeline.indicator.SERIES_IDS, retrieve.call_args.args[0])
        self.assertEqual("A01", retrieve.call_args.args[3])

    def test_missing_optional_set_retries_required_headlines(self):
        sentinel = BlsResult(
            observations={},
            source_organization="BLS",
            source_dataset="LABSTAT",
            source_url="https://example.invalid/bls",
            retrieval_method="api",
            retrieval_date="2026-08-13",
        )
        with mock.patch.object(
            pipeline.bls,
            "retrieve",
            side_effect=[pipeline.RetrievalError("optional series missing"), sentinel],
        ) as retrieve, mock.patch("sys.stderr"):
            result = pipeline.retrieve_bls_data()

        self.assertIs(sentinel, result)
        self.assertEqual(2, retrieve.call_count)
        self.assertEqual(
            pipeline.indicator.HEADLINE_SERIES_IDS,
            retrieve.call_args_list[1].args[0],
        )


def card_observation(
    year: int, value: str, sex: str | None = None, disability: str | None = None,
    age: str | None = None,
):
    disaggregation = {}
    if sex is not None:
        disaggregation["sex"] = sex
    if disability is not None:
        disaggregation["disability"] = disability
    if age is not None:
        disaggregation["age"] = age
    row = {
        "indicator_id": "8.5.2",
        "indicator_title": "Unemployment rate fixture",
        "year": str(year),
        "value": value,
        "unit": "percent",
        "geography": "United States",
        "disaggregation": "{}",
        "source_organization": "BLS",
        "source_url": "https://example.invalid/bls",
        "validation_status": "current_methodology_verified",
        "data_warning": "Age 16 qualification",
    }
    return cards.Observation(
        row=row,
        year=year,
        value=Decimal(value),
        disaggregation=disaggregation,
    )


class DataCard852Tests(unittest.TestCase):
    """Protect the four-value headline and exclusion of age-detail rows."""

    def test_card_selects_latest_four_headline_rates(self):
        observations = [
            card_observation(2024, "7.0", "Male", "With disability"),
            card_observation(2025, "8.4", "Male", "With disability"),
            card_observation(2025, "8.1", "Female", "With disability"),
            card_observation(2025, "4.2", "Male", "No disability"),
            card_observation(2025, "4.0", "Female", "No disability"),
            # A newer age-detail record must never replace a headline series.
            card_observation(2026, "99.9", disability="With disability", age="16-19"),
        ]

        metrics = cards.select_metrics("8.5.2", observations)

        self.assertEqual(
            [
                "Men with disability",
                "Women with disability",
                "Men without disability",
                "Women without disability",
            ],
            [metric.label for metric in metrics],
        )
        self.assertEqual(
            [Decimal("8.4"), Decimal("8.1"), Decimal("4.2"), Decimal("4.0")],
            [metric.observation.value for metric in metrics],
        )
        self.assertTrue(all(metric.observation.year == 2025 for metric in metrics))


if __name__ == "__main__":
    unittest.main()
