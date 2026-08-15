"""Offline tests for the reviewed UNSD comparison selection layer."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests.pipeline_test_utils import PROJECT_ROOT, load_pipeline_module
from sdg_pipeline import unsd_comparison as comparison
from sdg_pipeline.sources import unsd


pipeline = load_pipeline_module(
    "fetch_unsd_comparisons_for_tests", "fetch_unsd_comparisons.py"
)


def observation(
    indicator_ids,
    series,
    year,
    value,
    dimensions,
    *,
    nature="C",
    footnotes=(),
):
    return unsd.UnsdObservation(
        indicator_ids=tuple(indicator_ids),
        series_code=series,
        series_description=f"Fixture {series}",
        geography_code="840",
        geography_name="United States of America",
        year=year,
        value=Decimal(value),
        dimensions=dict(dimensions),
        attributes={"Nature": nature, "Units": "PERCENT", "Observation Status": "A"},
        source="Fixture official source",
        footnotes=tuple(footnotes),
    )


def result(observations):
    return unsd.UnsdResult(
        observations=tuple(observations),
        raw_observation_count=len(observations),
        deduplicated_observation_count=len(observations),
        source_organization=unsd.UNSD_SOURCE_ORGANIZATION,
        source_url="https://unstats.un.org/SDGAPI/test",
        retrieval_method="api",
        retrieval_date="2026-08-15",
        database_release="2026.Q2.G.01",
        database_last_updated="2026-07-07T13:46:06",
        attribute_descriptions={
            "Nature": {"C": "Country data", "CA": "Country adjusted data"},
            "Units": {"PERCENT": "Percentage"},
        },
        dimension_descriptions={},
    )


class RegistryTests(unittest.TestCase):
    """Protect the reviewed series, classifications, and shared components."""

    def test_registry_covers_exactly_13_indicators_with_reviewed_classifications(self):
        self.assertEqual(13, len(comparison.indicator_ids()))
        classifications = {
            indicator_id: {
                rule.comparison_status
                for rule in comparison.rules_for_indicator(indicator_id)
            }
            for indicator_id in comparison.indicator_ids()
        }
        self.assertEqual(
            {"3.1.2", "3.7.2", "8.5.2", "8.a.1", "10.b.1"},
            {
                indicator_id
                for indicator_id, values in classifications.items()
                if values == {comparison.DIRECTLY_COMPARABLE}
            },
        )
        self.assertEqual(
            {"15.a.1", "15.b.1"},
            {
                indicator_id
                for indicator_id, values in classifications.items()
                if values == {comparison.PARTIAL_COMPONENT}
            },
        )

    def test_repeat_and_multicomponent_series_are_explicit(self):
        fifteen_a = comparison.rules_for_indicator("15.a.1")
        fifteen_b = comparison.rules_for_indicator("15.b.1")
        self.assertEqual("DC_ODA_BDVDL", fifteen_a[0].series_code)
        self.assertEqual(fifteen_a[0].series_code, fifteen_b[0].series_code)
        self.assertEqual(
            {"DC_TOF_TRDCMDL", "DC_TOF_TRDDBMDL"},
            {rule.series_code for rule in comparison.rules_for_indicator("8.a.1")},
        )
        self.assertEqual(
            {"DC_ODA_TOTGGE", "DC_ODA_LDCG"},
            {rule.series_code for rule in comparison.rules_for_indicator("17.2.1")},
        )

    def test_refresh_failure_can_never_trigger_un_fallback(self):
        self.assertEqual(
            "use_reviewed_unsd_fallback_when_suitability_allows",
            comparison.FALLBACK_POLICY["underlying_us_data_unavailable"],
        )
        self.assertEqual(
            "retain_last_successful_us_observation",
            comparison.FALLBACK_POLICY["us_pipeline_refresh_failed"],
        )


class ComparisonBuilderTests(unittest.TestCase):
    """Protect component selection, completeness guards, JSON, and schema."""

    def test_component_selection_and_deterministic_disaggregation(self):
        rules = comparison.rules_for_indicator("8.a.1")
        observations = [
            observation(["8.a.1"], "DC_TOF_TRDCMDL", 2024, "5000", {"Reporting Type": "G"}),
            observation(["8.a.1"], "DC_TOF_TRDDBMDL", 2024, "3100", {"Reporting Type": "G"}),
            observation(["8.a.1"], "DC_TOF_TRDCML", 2024, "99", {"Reporting Type": "G"}),
        ]

        rows = comparison.build_comparison_rows(result(observations), rules)["8.a.1"]

        self.assertEqual(2, len(rows))
        self.assertEqual(
            {"donor_commitments", "donor_disbursements"},
            {row["comparison_component"] for row in rows},
        )
        self.assertTrue(all(row["disaggregation"] == '{"Reporting Type":"G"}' for row in rows))
        self.assertTrue(all(list(row) == comparison.COMPARISON_COLUMNS for row in rows))

    def test_10_b_1_guard_excludes_reviewed_incomplete_2025(self):
        rules = comparison.rules_for_indicator("10.b.1")
        observations = [
            observation(["10.b.1"], "DC_TRF_TOTDL", 2024, "231645.76", {"Reporting Type": "G"}),
            observation(["10.b.1"], "DC_TRF_TOTDL", 2025, "29021.89", {"Reporting Type": "G"}),
        ]

        rows = comparison.build_comparison_rows(result(observations), rules)["10.b.1"]
        by_year = {row["year"]: row for row in rows}

        self.assertEqual("true", by_year[2024]["is_preferred_comparison"])
        self.assertEqual("complete", by_year[2024]["completeness_status"])
        self.assertEqual("false", by_year[2025]["is_preferred_comparison"])
        self.assertEqual("apparently_incomplete", by_year[2025]["completeness_status"])

    def test_17_2_1_guard_excludes_provisional_total_but_keeps_ldc_component(self):
        rules = comparison.rules_for_indicator("17.2.1")
        observations = [
            observation(["17.2.1"], "DC_ODA_TOTGGE", 2024, "0.225381", {"Reporting Type": "G"}),
            observation(
                ["17.2.1"], "DC_ODA_TOTGGE", 2025, "0.094008",
                {"Reporting Type": "G"}, footnotes=("Provisional",),
            ),
            observation(["17.2.1"], "DC_ODA_LDCG", 2024, "0.05452", {"Reporting Type": "G"}),
        ]

        rows = comparison.build_comparison_rows(result(observations), rules)["17.2.1"]
        preferred = {
            row["comparison_component"]: row
            for row in rows
            if row["is_preferred_comparison"] == "true"
        }

        self.assertEqual(2024, preferred["total_oda_gni_grant_equivalent"]["year"])
        self.assertEqual(2024, preferred["ldc_oda_gni_net"]["year"])
        provisional = next(row for row in rows if row["year"] == 2025)
        self.assertEqual("provisional", provisional["completeness_status"])
        self.assertEqual('["Provisional"]', provisional["footnotes"])

    def test_comparison_schema_rejects_non_deterministic_json(self):
        row = {column: "" for column in comparison.COMPARISON_COLUMNS}
        row.update(
            {
                "disaggregation": '{"z":"1", "a":"2"}',
                "footnotes": "[]",
            }
        )
        with self.assertRaisesRegex(ValueError, "not deterministic"):
            comparison.validate_comparison_row(row)


class OutputIsolationTests(unittest.TestCase):
    """Prove comparison writing does not mutate standardized U.S. outputs."""

    @staticmethod
    def standardized_hashes():
        directory = PROJECT_ROOT / "data_processed" / "standardized"
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.glob("*.csv"))
        }

    def test_writing_all_comparisons_leaves_standardized_outputs_unchanged(self):
        before = self.standardized_hashes()
        template = {column: "" for column in comparison.COMPARISON_COLUMNS}
        template.update(
            {
                "comparison_component": "fixture",
                "series_code": "FIXTURE",
                "year": 2024,
                "value": "1",
                "geography": "United States of America",
                "geography_code": "840",
                "disaggregation": "{}",
                "footnotes": "[]",
                "is_preferred_comparison": "true",
            }
        )
        rows = {}
        for indicator_id in pipeline.INDICATOR_IDS:
            row = dict(template)
            row["indicator_id"] = indicator_id
            rows[indicator_id] = [row]

        with tempfile.TemporaryDirectory() as temporary_directory:
            pipeline.write_outputs(rows, output_dir=Path(temporary_directory))
            self.assertEqual(13, len(list(Path(temporary_directory).glob("*.csv"))))

        self.assertEqual(before, self.standardized_hashes())


if __name__ == "__main__":
    unittest.main()
