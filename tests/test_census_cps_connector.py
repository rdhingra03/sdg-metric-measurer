"""Focused offline tests for the reusable Census CPS connector."""

from __future__ import annotations

import io
import json
import unittest
import urllib.request
import zipfile

from tests.pipeline_test_utils import SRC_ROOT  # Makes the src package importable.
from sdg_pipeline.errors import RetrievalError
from sdg_pipeline.sources import census_cps


class CensusCpsConnectorTests(unittest.TestCase):
    """Protect generic API, fallback, fixed-width, and layout behavior."""

    def test_successful_api_parsing(self) -> None:
        variables = ("PRTAGE", "PESEX", "PWSUPWGT")

        def execute(request: urllib.request.Request, display_url: str):
            self.assertNotIn("fixture-key", display_url)
            self.assertIn("PRTAGE=5", request.full_url)
            payload = [
                list(variables),
                ["5", "1", "12340000"],
                ["5", "2", "23450000"],
            ]
            return json.dumps(payload).encode(), "application/json"

        observations = census_cps.fetch_from_api(
            2022,
            "fixture-key",
            variables,
            query_filters={"PRTAGE": 5},
            request_executor=execute,
        )

        self.assertEqual(2, len(observations))
        self.assertEqual(5, observations[0].value("PRTAGE"))
        self.assertEqual(12_340_000, observations[0].value("PWSUPWGT"))

    def test_api_failure_uses_download_fallback(self) -> None:
        fallback_observations = [census_cps.CpsObservation({"PRTAGE": 5})]
        fallback_url = "https://example.invalid/official-cps.zip"

        def failed_api():
            raise RetrievalError("simulated API outage")

        result = census_cps.retrieve_year(
            2022,
            ("PRTAGE",),
            api_key="fixture-key",
            api_fetcher=failed_api,
            download_fetcher=lambda: (fallback_observations, fallback_url),
            retrieval_date="2026-08-09",
        )

        self.assertIs(fallback_observations, result.observations)
        self.assertEqual("download", result.retrieval_method)
        self.assertEqual(fallback_url, result.source_url)
        self.assertEqual(1, len(result.source_warnings))

    def test_zip_fixed_width_parsing_with_small_fixture(self) -> None:
        layout = census_cps.FixedWidthLayout(
            name="small test layout",
            minimum_record_length=10,
            fields={
                "PRTAGE": census_cps.FieldPosition(1, 2),
                "PESEX": census_cps.FieldPosition(3, 3),
                "PESCH35": census_cps.FieldPosition(4, 4),
                "PECHGRDE": census_cps.FieldPosition(5, 6),
                "PWSUPWGT": census_cps.FieldPosition(7, 10),
            },
        )
        config = census_cps.DownloadConfig(
            url="https://example.invalid/official-cps.zip",
            file_format="zip",
            layout=layout,
            archive_member="fixture.dat",
        )
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, mode="w") as archive:
            archive.writestr("fixture.dat", b"0511030123\n0622040456\n")

        observations, source_url = census_cps.fetch_from_download(
            2022,
            ("PRTAGE", "PESEX", "PESCH35", "PECHGRDE", "PWSUPWGT"),
            download_configs={2022: config},
            record_filters={"PRTAGE": 5},
            request_executor=lambda _request, _display_url: (
                archive_bytes.getvalue(),
                "application/zip",
            ),
        )

        self.assertEqual(config.url, source_url)
        self.assertEqual(1, len(observations))
        self.assertEqual(1, observations[0].value("PESEX"))
        self.assertEqual(3, observations[0].value("PECHGRDE"))
        self.assertEqual(123, observations[0].value("PWSUPWGT"))

    def test_historical_weight_selection_and_implied_scale(self) -> None:
        self.assertEqual("PWSSWGT", census_cps.weight_variable_for_year(2005))
        self.assertEqual("PWSUPWGT", census_cps.weight_variable_for_year(2006))
        self.assertEqual(10_000, census_cps.WEIGHT_SCALE)

    def test_missing_required_variable_is_detected(self) -> None:
        payload = [["PRTAGE", "PESEX"], ["5", "1"]]
        with self.assertRaisesRegex(RetrievalError, "PWSUPWGT"):
            census_cps.fetch_from_api(
                2022,
                "fixture-key",
                ("PRTAGE", "PESEX", "PWSUPWGT"),
                request_executor=lambda _request, _display_url: (
                    json.dumps(payload).encode(),
                    "application/json",
                ),
            )


if __name__ == "__main__":
    unittest.main()
