"""Reusable retrieval for Bureau of Labor Statistics LABSTAT series."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Dict, Iterable, Mapping, Sequence, Tuple

from ..errors import RetrievalError, SourceValidationError
from ..http import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    PROJECT_USER_AGENT,
    request_bytes as shared_request_bytes,
)
from ..output import current_retrieval_date


BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_BULK_DATA_URL = (
    "https://downloadt.bls.gov/pub/time.series/ln/ln.data.1.AllData"
)
BLS_SOURCE_ORGANIZATION = "U.S. Bureau of Labor Statistics"
BLS_SOURCE_DATASET = "LABSTAT Labor Force Statistics from the Current Population Survey"
API_YEAR_CHUNK = 10
HTTP_TIMEOUT_SECONDS = DEFAULT_HTTP_TIMEOUT_SECONDS
USER_AGENT = f"{PROJECT_USER_AGENT} (official BLS public data client)"

Observations = Dict[str, Dict[int, Decimal]]
RequestExecutor = Callable[[urllib.request.Request], Tuple[bytes, str]]
ObservationFetcher = Callable[[], Observations]
WarningHandler = Callable[[str], None]


@dataclass(frozen=True)
class BlsResult:
    """BLS observations together with the provenance of the successful source."""

    observations: Observations
    source_organization: str
    source_dataset: str
    source_url: str
    retrieval_method: str
    retrieval_date: str
    source_warnings: tuple[str, ...] = ()


def request_bytes(request: urllib.request.Request) -> Tuple[bytes, str]:
    """Execute a BLS API request through the shared credential-safe helper."""

    return shared_request_bytes(
        request, display_url=BLS_API_URL, timeout=HTTP_TIMEOUT_SECONDS
    )


def year_chunks(
    start_year: int, end_year: int, chunk_size: int = API_YEAR_CHUNK
) -> Iterable[Tuple[int, int]]:
    """Yield inclusive BLS API year ranges of at most ``chunk_size`` years."""

    if start_year > end_year:
        raise ValueError("start_year cannot be later than end_year")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    chunk_start = start_year
    while chunk_start <= end_year:
        chunk_end = min(chunk_start + chunk_size - 1, end_year)
        yield chunk_start, chunk_end
        chunk_start = chunk_end + 1


def add_observation(
    observations: Observations,
    series_id: str,
    year_text: object,
    value_text: object,
    source_name: str,
    required_period: str,
) -> None:
    """Parse one observation and reject conflicting duplicates."""

    try:
        year = int(str(year_text))
        value = Decimal(str(value_text).replace(",", "").strip())
    except (ValueError, ArithmeticError) as error:
        raise SourceValidationError(
            f"Invalid {source_name} observation for {series_id}: "
            f"year={year_text!r}, value={value_text!r}"
        ) from error

    existing = observations[series_id].get(year)
    if existing is not None and existing != value:
        raise SourceValidationError(
            f"Conflicting {source_name} {required_period} observations for "
            f"{series_id}, {year}"
        )
    observations[series_id][year] = value


def validate_observations(
    observations: Mapping[str, Mapping[int, Decimal]],
    series_ids: Sequence[str],
    source_name: str,
    required_period: str,
) -> None:
    """Require matching, continuous observations for every requested series."""

    missing_series = [series_id for series_id in series_ids if not observations[series_id]]
    if missing_series:
        raise SourceValidationError(
            f"{source_name} has no {required_period} observations for: "
            + ", ".join(missing_series)
        )

    year_sets = {series_id: set(observations[series_id]) for series_id in series_ids}
    all_years = set().union(*year_sets.values())
    incomplete = {
        year: [series_id for series_id in series_ids if year not in year_sets[series_id]]
        for year in sorted(all_years)
        if any(year not in year_sets[series_id] for series_id in series_ids)
    }
    if incomplete:
        details = "; ".join(
            f"{year}: {', '.join(missing_ids)}"
            for year, missing_ids in list(incomplete.items())[:10]
        )
        raise SourceValidationError(
            f"{source_name} is missing required annual observations ({details})"
        )

    first_year, last_year = min(all_years), max(all_years)
    missing_years = sorted(set(range(first_year, last_year + 1)) - all_years)
    if missing_years:
        raise SourceValidationError(
            f"{source_name} annual observations have gaps: "
            + ", ".join(map(str, missing_years))
        )


def fetch_from_api(
    series_ids: Sequence[str],
    start_year: int,
    end_year: int,
    required_period: str,
    *,
    request_executor: RequestExecutor = request_bytes,
    chunker: Callable[[int, int], Iterable[Tuple[int, int]]] = year_chunks,
) -> Observations:
    """Fetch and validate requested period observations from the BLS API."""

    observations: Observations = {series_id: {} for series_id in series_ids}
    for chunk_start, chunk_end in chunker(start_year, end_year):
        payload = json.dumps(
            {
                "seriesid": list(series_ids),
                "startyear": str(chunk_start),
                "endyear": str(chunk_end),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            BLS_API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        body, content_type = request_executor(request)

        if content_type != "application/json" or body.lstrip().startswith(b"<"):
            preview = body.lstrip()[:80].decode("utf-8", errors="replace")
            raise SourceValidationError(
                "BLS API returned a non-JSON response "
                f"({content_type!r}; starts with {preview!r})"
            )
        try:
            response_data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SourceValidationError("BLS API returned invalid JSON") from error

        if not isinstance(response_data, dict):
            raise SourceValidationError("BLS API returned an unexpected JSON structure")
        if response_data.get("status") != "REQUEST_SUCCEEDED":
            message = "; ".join(response_data.get("message") or [])
            raise SourceValidationError(
                f"BLS API request was unsuccessful: {message}"
            )

        results = response_data.get("Results")
        if not isinstance(results, dict):
            raise SourceValidationError("BLS API response has malformed Results")
        returned_series = results.get("series", [])
        if not isinstance(returned_series, list):
            raise SourceValidationError("BLS API response has malformed Results.series")
        returned_ids = {
            series.get("seriesID")
            for series in returned_series
            if isinstance(series, dict)
        }
        missing_ids = sorted(set(series_ids) - returned_ids)
        if missing_ids:
            raise SourceValidationError(
                "BLS API response omitted required series: " + ", ".join(missing_ids)
            )

        for series in returned_series:
            if not isinstance(series, dict):
                raise SourceValidationError("BLS API returned a malformed series")
            series_id = series.get("seriesID")
            if series_id not in observations:
                continue
            data = series.get("data", [])
            if not isinstance(data, list):
                raise SourceValidationError(
                    f"BLS API returned malformed data for {series_id}"
                )
            for item in data:
                if not isinstance(item, dict):
                    raise SourceValidationError(
                        f"BLS API returned a malformed observation for {series_id}"
                    )
                if item.get("period") != required_period:
                    continue
                add_observation(
                    observations,
                    series_id,
                    item.get("year", ""),
                    item.get("value", ""),
                    "BLS API",
                    required_period,
                )

    validate_observations(
        observations, series_ids, "BLS API", required_period
    )
    return observations


def parse_bulk_lines(
    lines: Iterable[bytes], series_ids: Sequence[str], required_period: str
) -> Observations:
    """Parse only requested series and period records from LABSTAT bulk lines."""

    observations: Observations = {series_id: {} for series_id in series_ids}
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) < 4:
            continue
        series_id, year, period, value = fields[:4]
        if series_id in observations and period == required_period:
            add_observation(
                observations,
                series_id,
                year,
                value,
                "BLS bulk data",
                required_period,
            )
    validate_observations(
        observations, series_ids, "BLS bulk data", required_period
    )
    return observations


def fetch_from_bulk_data(
    series_ids: Sequence[str], required_period: str
) -> Observations:
    """Stream requested records from the official large LABSTAT bulk file."""

    request = urllib.request.Request(
        BLS_BULK_DATA_URL, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return parse_bulk_lines(response, series_ids, required_period)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        raise RetrievalError(
            f"Official BLS bulk-data request failed: {error}"
        ) from error


def retrieve(
    series_ids: Sequence[str],
    start_year: int,
    end_year: int,
    required_period: str,
    *,
    api_fetcher: ObservationFetcher | None = None,
    bulk_fetcher: ObservationFetcher | None = None,
    warning_handler: WarningHandler | None = None,
    retrieval_date: str | None = None,
) -> BlsResult:
    """Prefer the BLS API and fall back to official LABSTAT bulk data."""

    api_fetcher = api_fetcher or (
        lambda: fetch_from_api(series_ids, start_year, end_year, required_period)
    )
    bulk_fetcher = bulk_fetcher or (
        lambda: fetch_from_bulk_data(series_ids, required_period)
    )
    warnings = []
    try:
        observations = api_fetcher()
        method = "api"
        source_url = BLS_API_URL
    except RetrievalError as api_error:
        warning = (
            f"BLS API unavailable or invalid: {api_error}\n"
            "Trying the official BLS LN bulk-data fallback..."
        )
        warnings.append(warning)
        if warning_handler is not None:
            warning_handler(warning)
        try:
            observations = bulk_fetcher()
            method = "bulk"
            source_url = BLS_BULK_DATA_URL
        except RetrievalError as bulk_error:
            raise RetrievalError(
                "Both official retrieval methods failed. "
                f"API error: {api_error}. Bulk-data error: {bulk_error}"
            ) from bulk_error

    return BlsResult(
        observations=observations,
        source_organization=BLS_SOURCE_ORGANIZATION,
        source_dataset=BLS_SOURCE_DATASET,
        source_url=source_url,
        retrieval_method=method,
        retrieval_date=retrieval_date or current_retrieval_date(),
        source_warnings=tuple(warnings),
    )
