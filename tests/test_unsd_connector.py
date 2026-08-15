"""Offline tests for the generic UNSD SDG API connector."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import unittest
from decimal import Decimal
from unittest import mock

from tests.pipeline_test_utils import FIXTURE_ROOT, SRC_ROOT  # Makes src importable.
from sdg_pipeline.errors import SourceValidationError
from sdg_pipeline.sources import unsd


def fixture(name: str) -> bytes:
    return (FIXTURE_ROOT / name).read_bytes()


class FixtureExecutor:
    """Route connector requests to small checked-in JSON fixtures."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, _request, display_url):
        self.urls.append(display_url)
        if display_url == unsd.INDICATOR_LIST_ENDPOINT:
            return fixture("unsd_catalog.json"), "application/json"
        if display_url == unsd.SERIES_LAST_UPDATED_ENDPOINT:
            return fixture("unsd_last_updated.json"), "application/json"
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(display_url).query)
        page = query.get("page", [""])[0]
        if page == "1":
            return fixture("unsd_page_1.json"), "application/json"
        if page == "2":
            return fixture("unsd_page_2.json"), "application/json"
        raise AssertionError(f"Unexpected fixture URL: {display_url}")


class FakeHeaders:
    def get_content_type(self):
        return "application/json"


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.headers = FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class UnsdConnectorTests(unittest.TestCase):
    """Protect pagination, provenance, parsing, validation, and retries."""

    def test_multi_indicator_pagination_and_exact_duplicate_removal(self):
        executor = FixtureExecutor()

        result = unsd.fetch_indicator_observations(
            ["3.1.2", "8.6.1"],
            request_executor=executor,
            retrieval_date="2026-08-15",
            page_size=2,
        )

        self.assertEqual(4, result.raw_observation_count)
        self.assertEqual(3, result.deduplicated_observation_count)
        self.assertEqual("2026.Q2.G.01", result.database_release)
        self.assertEqual("2026-07-07T13:46:06", result.database_last_updated)
        self.assertEqual("2026-08-15", result.retrieval_date)
        self.assertIn("Removed 1 exact duplicate", result.warnings[0])
        data_urls = [url for url in executor.urls if "Indicator/Data" in url]
        self.assertEqual(2, len(data_urls))
        first_query = urllib.parse.parse_qs(urllib.parse.urlsplit(data_urls[0]).query)
        self.assertEqual(["3.1.2", "8.6.1"], first_query["indicator"])
        self.assertEqual(["840"], first_query["areaCode"])
        self.assertEqual(["1"], first_query["page"])
        self.assertEqual(["2"], urllib.parse.parse_qs(urllib.parse.urlsplit(data_urls[1]).query)["page"])

    def test_response_preserves_nature_status_dimensions_source_and_footnotes(self):
        result = unsd.fetch_indicator_observations(
            ["3.1.2", "8.6.1"],
            request_executor=FixtureExecutor(),
        )
        observation = next(
            item
            for item in result.observations
            if item.series_code == "SL_TLF_NEET_19ICLS"
        )

        self.assertEqual(Decimal("12.34"), observation.value)
        self.assertEqual("C", observation.attributes["Nature"])
        self.assertEqual("A", observation.attributes["Observation Status"])
        self.assertEqual("15-24", observation.dimensions["Age"])
        self.assertEqual("LFS - Current Population Survey", observation.source)
        self.assertIn("minimum age: 16", observation.footnotes[0])
        self.assertEqual("Country data", result.attribute_description("Nature", "C"))

    def test_malformed_response_is_rejected(self):
        with self.assertRaisesRegex(SourceValidationError, "data list"):
            unsd.parse_data_page(
                {"pageNumber": 1, "totalPages": 1, "totalElements": 0},
                expected_page=1,
                expected_area_code="840",
            )

    def test_wrong_geography_is_rejected(self):
        payload = json.loads(fixture("unsd_page_1.json"))
        payload["data"][0]["geoAreaCode"] = "124"
        with self.assertRaisesRegex(SourceValidationError, "expected '840'"):
            unsd.parse_data_page(
                payload,
                expected_page=1,
                expected_area_code="840",
            )

    def test_temporary_http_failure_uses_shared_retry_then_succeeds(self):
        request = urllib.request.Request("https://unstats.un.org/SDGAPI/test")
        failure = urllib.error.HTTPError(
            request.full_url, 503, "Service Unavailable", {}, None
        )
        response = FakeResponse(b"{}")
        with mock.patch(
            "sdg_pipeline.http.urllib.request.urlopen",
            side_effect=[failure, response],
        ) as open_url, mock.patch("sdg_pipeline.http.time.sleep"):
            body, content_type = unsd.request_bytes(request, request.full_url)

        self.assertEqual(b"{}", body)
        self.assertEqual("application/json", content_type)
        self.assertEqual(2, open_url.call_count)


if __name__ == "__main__":
    unittest.main()
