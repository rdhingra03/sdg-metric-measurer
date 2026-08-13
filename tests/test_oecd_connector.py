"""Focused offline tests for the reusable OECD SDMX connector."""

from __future__ import annotations

import unittest
from decimal import Decimal

from tests.pipeline_test_utils import SRC_ROOT  # Makes the src package importable.
from sdg_pipeline.errors import RetrievalError, SourceValidationError
from sdg_pipeline.sources import oecd


CSV_HEADER = "DONOR,SCORE,BASE_PER,TIME_PERIOD,OBS_VALUE\n"


def query(required_years=(2022,)) -> oecd.SdmxQuery:
    """Return a small generic query definition used by connector tests."""

    return oecd.SdmxQuery(
        agency_id="OECD.TEST",
        dataflow_id="DSD_TEST@DF_TEST",
        version="1.0",
        key="USA.1+2",
        start_year=min(required_years),
        end_year=max(required_years),
        source_dataset="Test OECD dataset",
        dimension_columns=("DONOR", "SCORE"),
        expected_dimensions={
            "DONOR": frozenset({"USA"}),
            "SCORE": frozenset({"1", "2"}),
            "BASE_PER": frozenset({"2024"}),
        },
        required_years=tuple(required_years),
    )


class OecdConnectorTests(unittest.TestCase):
    """Protect parsing, dimension checks, duplicates, gaps, and failures."""

    def test_valid_sdmx_csv_parsing_preserves_decimal_precision(self) -> None:
        body = (
            CSV_HEADER
            + "USA,1,2024,2022,231.791554\n"
            + "USA,2,2024,2022,545.313317\n"
        ).encode()

        observations, warnings = oecd.parse_sdmx_csv(body, query())

        self.assertEqual(2, len(observations))
        self.assertEqual(Decimal("231.791554"), observations[0].value)
        self.assertEqual("1", observations[0].dimension("SCORE"))
        self.assertEqual((), warnings)

    def test_required_dimension_validation(self) -> None:
        body = (CSV_HEADER + "CAN,1,2024,2022,1.0\n").encode()

        with self.assertRaisesRegex(SourceValidationError, "DONOR='CAN'"):
            oecd.parse_sdmx_csv(body, query())

    def test_identical_duplicate_is_safely_deduplicated(self) -> None:
        body = (
            CSV_HEADER
            + "USA,1,2024,2022,10.25\n"
            + "USA,1,2024,2022,10.25\n"
        ).encode()

        observations, warnings = oecd.parse_sdmx_csv(body, query())

        self.assertEqual(1, len(observations))
        self.assertEqual(1, len(warnings))
        self.assertIn("identical duplicate", warnings[0])

    def test_conflicting_duplicate_is_rejected(self) -> None:
        body = (
            CSV_HEADER
            + "USA,1,2024,2022,10.25\n"
            + "USA,1,2024,2022,10.26\n"
        ).encode()

        with self.assertRaisesRegex(SourceValidationError, "conflicting duplicate"):
            oecd.parse_sdmx_csv(body, query())

    def test_missing_required_year_is_rejected(self) -> None:
        body = (CSV_HEADER + "USA,1,2024,2022,10.25\n").encode()

        with self.assertRaisesRegex(
            SourceValidationError, "missing required annual observations: 2023"
        ):
            oecd.parse_sdmx_csv(body, query((2022, 2023)))

    def test_http_failure_propagates_as_retrieval_error(self) -> None:
        def failed_request(_request, _display_url):
            raise RetrievalError("simulated OECD outage")

        with self.assertRaisesRegex(RetrievalError, "simulated OECD outage"):
            oecd.fetch_sdmx_csv(query(), request_executor=failed_request)

    def test_fetch_records_provenance_and_exact_url(self) -> None:
        body = (CSV_HEADER + "USA,1,2024,2022,10.25\n").encode()

        result = oecd.fetch_sdmx_csv(
            query(),
            request_executor=lambda _request, _url: (body, "text/csv"),
            retrieval_date="2026-08-11",
        )

        self.assertEqual("api", result.retrieval_method)
        self.assertEqual("1.0", result.dataflow_version)
        self.assertIn("USA.1%2B2", result.source_url)
        self.assertEqual("2026-08-11", result.retrieval_date)


if __name__ == "__main__":
    unittest.main()
