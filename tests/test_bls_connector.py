"""Focused offline tests for the reusable BLS source connector."""

from __future__ import annotations

import json
import unittest
import urllib.request
from decimal import Decimal

from tests.pipeline_test_utils import SRC_ROOT  # Makes the src package importable.
from sdg_pipeline.errors import RetrievalError
from sdg_pipeline.sources import bls


class BlsConnectorTests(unittest.TestCase):
    """Protect generic API parsing, period selection, and fallback behavior."""

    def test_successful_api_parsing(self) -> None:
        def execute(request: urllib.request.Request):
            request_payload = json.loads(request.data)
            self.assertEqual(["SERIES_A", "SERIES_B"], request_payload["seriesid"])
            payload = {
                "status": "REQUEST_SUCCEEDED",
                "Results": {
                    "series": [
                        {
                            "seriesID": "SERIES_A",
                            "data": [
                                {"year": "2022", "period": "M13", "value": "10"}
                            ],
                        },
                        {
                            "seriesID": "SERIES_B",
                            "data": [
                                {"year": "2022", "period": "M13", "value": "20"}
                            ],
                        },
                    ]
                },
            }
            return json.dumps(payload).encode(), "application/json"

        observations = bls.fetch_from_api(
            ("SERIES_A", "SERIES_B"),
            2022,
            2022,
            "M13",
            request_executor=execute,
        )

        self.assertEqual({2022: Decimal("10")}, observations["SERIES_A"])
        self.assertEqual({2022: Decimal("20")}, observations["SERIES_B"])

    def test_html_api_response_is_rejected(self) -> None:
        with self.assertRaisesRegex(RetrievalError, "non-JSON response"):
            bls.fetch_from_api(
                ("SERIES_A",),
                2022,
                2022,
                "M13",
                request_executor=lambda _request: (
                    b"<html>maintenance</html>",
                    "text/html",
                ),
            )

    def test_api_failure_uses_bulk_fallback_and_records_provenance(self) -> None:
        expected = {"SERIES_A": {2022: Decimal("10")}}

        def failed_api():
            raise RetrievalError("simulated API outage")

        result = bls.retrieve(
            ("SERIES_A",),
            2022,
            2022,
            "M13",
            api_fetcher=failed_api,
            bulk_fetcher=lambda: expected,
            retrieval_date="2026-08-09",
        )

        self.assertIs(expected, result.observations)
        self.assertEqual("bulk", result.retrieval_method)
        self.assertEqual(bls.BLS_BULK_DATA_URL, result.source_url)
        self.assertEqual("2026-08-09", result.retrieval_date)
        self.assertEqual(1, len(result.source_warnings))

    def test_bulk_parser_selects_only_requested_period(self) -> None:
        lines = [
            b"series_id\tyear\tperiod\tvalue\n",
            b"SERIES_A\t2022\tM01\t99\n",
            b"SERIES_A\t2022\tM13\t10\n",
        ]

        observations = bls.parse_bulk_lines(lines, ("SERIES_A",), "M13")

        self.assertEqual({2022: Decimal("10")}, observations["SERIES_A"])

    def test_api_detects_missing_requested_series(self) -> None:
        payload = {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [
                    {
                        "seriesID": "SERIES_A",
                        "data": [
                            {"year": "2022", "period": "M13", "value": "10"}
                        ],
                    }
                ]
            },
        }
        with self.assertRaisesRegex(RetrievalError, "SERIES_B"):
            bls.fetch_from_api(
                ("SERIES_A", "SERIES_B"),
                2022,
                2022,
                "M13",
                request_executor=lambda _request: (
                    json.dumps(payload).encode(),
                    "application/json",
                ),
            )


if __name__ == "__main__":
    unittest.main()

