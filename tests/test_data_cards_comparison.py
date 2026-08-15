"""Focused tests for the reviewed UN comparison layer in the data cards."""

from __future__ import annotations

import csv
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from tests.pipeline_test_utils import load_pipeline_module


cards = load_pipeline_module("build_data_cards_with_comparisons", "build_data_cards.py")


class DataCardComparisonTests(unittest.TestCase):
    def build_all_cards(self):
        built = []
        for indicator_id in cards.INDICATOR_ORDER:
            observations = cards.read_observations(
                indicator_id, cards.INPUT_FILES[indicator_id]
            )
            comparisons = cards.read_comparison_observations(
                indicator_id, cards.COMPARISON_FILES[indicator_id]
            )
            built.append(cards.build_card(indicator_id, observations, comparisons))
        return built

    def test_all_13_cards_have_one_reviewed_comparison(self):
        built = self.build_all_cards()

        self.assertEqual(13, len(built))
        self.assertEqual(13, len({card.indicator_id for card in built}))
        self.assertTrue(
            all(metric.comparison is not None for card in built for metric in card.metrics)
        )
        self.assertEqual(
            {
                "directly_comparable": 5,
                "comparable_with_methodology_difference": 6,
                "partial_component_comparison": 2,
            },
            dict(Counter(card.comparison_status for card in built)),
        )

    def test_multi_metric_components_are_matched_in_us_display_order(self):
        expected = {
            "3.7.2": ["ages_15_19", "ages_10_14"],
            "8.5.2": [
                "male_with_disability",
                "female_with_disability",
                "male_without_disability",
                "female_without_disability",
            ],
            "8.a.1": ["donor_commitments", "donor_disbursements"],
            "17.2.1": ["total_oda_gni_grant_equivalent", "ldc_oda_gni_net"],
        }
        by_indicator = {card.indicator_id: card for card in self.build_all_cards()}

        for indicator_id, expected_components in expected.items():
            self.assertEqual(
                expected_components,
                [metric.comparison.component for metric in by_indicator[indicator_id].metrics],
            )

    def test_reviewed_2024_finance_rows_are_used_instead_of_flagged_2025(self):
        by_indicator = {card.indicator_id: card for card in self.build_all_cards()}

        self.assertEqual(2024, by_indicator["10.b.1"].metrics[0].comparison.year)
        self.assertEqual(
            [2024, 2024],
            [metric.comparison.year for metric in by_indicator["17.2.1"].metrics],
        )
        rendered = cards.render_page(list(by_indicator.values()))
        self.assertNotIn("29021.89", rendered)
        self.assertNotIn("0.094008", rendered)

    def test_preferred_incomplete_row_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "comparison.csv"
            fieldnames = sorted(cards.COMPARISON_REQUIRED_FIELDS)
            row = {field: "" for field in fieldnames}
            row.update(
                {
                    "indicator_id": "3.1.2",
                    "comparison_component": "national",
                    "year": "2025",
                    "value": "99",
                    "unit": "Percentage",
                    "geography": "United States of America",
                    "disaggregation": "{}",
                    "comparison_status": "directly_comparable",
                    "completeness_status": "apparently_incomplete",
                    "is_preferred_comparison": "true",
                }
            )
            with path.open("w", encoding="utf-8", newline="") as output_file:
                writer = csv.DictWriter(output_file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(row)

            with self.assertRaisesRegex(ValueError, "apparently_incomplete"):
                cards.read_comparison_observations("3.1.2", path)

    def test_rendered_values_are_file_derived_and_differences_are_absolute(self):
        built = self.build_all_cards()
        rendered = cards.render_page(built)

        for card in built:
            for metric in card.metrics:
                self.assertIn(
                    f'data-raw-value="{metric.observation.row["value"]}"', rendered
                )
                self.assertIn(
                    f'data-comparison-raw-value="{metric.comparison.row["value"]}"',
                    rendered,
                )
                difference = metric.observation.value - metric.comparison.value
                if cards.format_difference(metric) is not None:
                    self.assertIn(f'data-us-minus-un="{difference}"', rendered)
        self.assertNotIn("percent difference", rendered.lower())

    def test_cross_year_comparisons_do_not_show_a_numeric_difference(self):
        by_indicator = {card.indicator_id: card for card in self.build_all_cards()}

        self.assertIsNone(cards.format_difference(by_indicator["3.1.2"].metrics[0]))
        self.assertIsNotNone(cards.format_difference(by_indicator["8.5.2"].metrics[0]))

    def test_partial_cards_use_reviewed_oda_component_label(self):
        by_indicator = {card.indicator_id: card for card in self.build_all_cards()}

        self.assertEqual(
            "ODA component comparison", by_indicator["15.a.1"].comparison_label
        )
        self.assertEqual(
            "ODA component comparison", by_indicator["15.b.1"].comparison_label
        )

    def test_generated_page_is_self_contained_and_has_each_card_once(self):
        rendered = cards.render_page(self.build_all_cards())

        self.assertEqual(13, rendered.count('<article class="indicator-card"'))
        for indicator_id in cards.INDICATOR_ORDER:
            self.assertEqual(
                1, rendered.count(f'data-indicator-id="{indicator_id}"')
            )
        self.assertIn("with official UN comparisons where available", rendered)
        self.assertNotIn("<script src=", rendered)
        self.assertNotIn("<link rel=", rendered)


if __name__ == "__main__":
    unittest.main()
