#!/usr/bin/env python3
"""Fetch and calculate the legacy U.S. proxy for SDG indicator 8.6.1.

This pipeline reproduces the archived U.S. implementation: the percentage of
people ages 16--24 who are not enrolled in school and are not employed.  This
is a U.S.-adapted proxy.  It differs from the current global SDG 8.6.1
definition, which measures people ages 15--24 not in employment, education, or
training (NEET).

The BLS Public Data API is tried first.  If the API is unavailable or returns
an invalid response (for example, an HTML maintenance page), the script reads
the official BLS Labor Force Statistics bulk data instead.  Only M13 annual
averages are used.

Only Python's standard library is required.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
CANONICAL_DATA_PATH = "sdg-master/data/indicator_8-6-1.csv"
OUTPUT_PATH = PROJECT_ROOT / "data_processed" / "sdg_8_6_1.csv"

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
# downloadt.bls.gov is an official BLS bulk-download host.  It provides the
# same LN files listed under download.bls.gov and is useful during API outages.
BLS_BULK_DATA_URL = (
    "https://downloadt.bls.gov/pub/time.series/ln/ln.data.1.AllData"
)

ENROLLED_SERIES = "LNU00022967"
NOT_ENROLLED_SERIES = "LNU00023016"
EMPLOYED_NOT_ENROLLED_SERIES = "LNU02023016"
SERIES_IDS = (
    ENROLLED_SERIES,
    NOT_ENROLLED_SERIES,
    EMPLOYED_NOT_ENROLLED_SERIES,
)

FIRST_BLS_YEAR = 1985
API_YEAR_CHUNK = 10
HTTP_TIMEOUT_SECONDS = 180
USER_AGENT = "sdg-metric-measurer/1.0 (official BLS public data client)"
ONE_DECIMAL = Decimal("0.1")

OUTPUT_COLUMNS = [
    "year",
    "enrolled_population_thousands",
    "not_enrolled_population_thousands",
    "employed_not_enrolled_thousands",
    "calculated_value",
    "source_method",
    "retrieval_date",
]


class RetrievalError(RuntimeError):
    """Raised when an official source cannot provide a complete dataset."""


def request_bytes(request: urllib.request.Request) -> Tuple[bytes, str]:
    """Return an HTTP response body and content type with a bounded timeout."""

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read(), response.headers.get_content_type()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        raise RetrievalError(f"Request failed for {request.full_url}: {error}") from error


def year_chunks(start_year: int, end_year: int) -> Iterable[Tuple[int, int]]:
    """Yield API-safe inclusive year ranges of at most ten years."""

    chunk_start = start_year
    while chunk_start <= end_year:
        chunk_end = min(chunk_start + API_YEAR_CHUNK - 1, end_year)
        yield chunk_start, chunk_end
        chunk_start = chunk_end + 1


def fetch_from_api() -> Dict[str, Dict[int, Decimal]]:
    """Fetch M13 observations for all required series from the BLS API."""

    observations: Dict[str, Dict[int, Decimal]] = {
        series_id: {} for series_id in SERIES_IDS
    }

    for start_year, end_year in year_chunks(FIRST_BLS_YEAR, date.today().year):
        payload = json.dumps(
            {
                "seriesid": list(SERIES_IDS),
                "startyear": str(start_year),
                "endyear": str(end_year),
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
        body, content_type = request_bytes(request)

        # A maintenance page can arrive with HTTP 200.  Check both the declared
        # content type and the first non-whitespace character before parsing.
        if content_type != "application/json" or body.lstrip().startswith(b"<"):
            preview = body.lstrip()[:80].decode("utf-8", errors="replace")
            raise RetrievalError(
                "BLS API returned a non-JSON response "
                f"({content_type!r}; starts with {preview!r})"
            )

        try:
            response_data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RetrievalError("BLS API returned invalid JSON") from error

        if response_data.get("status") != "REQUEST_SUCCEEDED":
            message = "; ".join(response_data.get("message") or [])
            raise RetrievalError(f"BLS API request was unsuccessful: {message}")

        returned_series = response_data.get("Results", {}).get("series", [])
        returned_ids = {series.get("seriesID") for series in returned_series}
        missing_ids = sorted(set(SERIES_IDS) - returned_ids)
        if missing_ids:
            raise RetrievalError(
                "BLS API response omitted required series: " + ", ".join(missing_ids)
            )

        for series in returned_series:
            series_id = series.get("seriesID")
            if series_id not in observations:
                continue
            for item in series.get("data", []):
                if item.get("period") != "M13":
                    continue
                add_observation(
                    observations,
                    series_id,
                    item.get("year", ""),
                    item.get("value", ""),
                    "BLS API",
                )

    validate_observations(observations, "BLS API")
    return observations


def fetch_from_bulk_data() -> Dict[str, Dict[int, Decimal]]:
    """Stream M13 records from the official BLS LN bulk data file."""

    observations: Dict[str, Dict[int, Decimal]] = {
        series_id: {} for series_id in SERIES_IDS
    }
    request = urllib.request.Request(
        BLS_BULK_DATA_URL,
        headers={"User-Agent": USER_AGENT},
    )

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            # The file is large, so process it line by line instead of holding
            # hundreds of megabytes in memory or saving a temporary copy.
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                fields = [field.strip() for field in line.split("\t")]
                if len(fields) < 4:
                    continue
                series_id, year, period, value = fields[:4]
                if series_id in observations and period == "M13":
                    add_observation(
                        observations, series_id, year, value, "BLS bulk data"
                    )
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        raise RetrievalError(
            f"Official BLS bulk-data request failed: {error}"
        ) from error

    validate_observations(observations, "BLS bulk data")
    return observations


def add_observation(
    observations: Dict[str, Dict[int, Decimal]],
    series_id: str,
    year_text: object,
    value_text: object,
    source_name: str,
) -> None:
    """Parse and add one annual observation, rejecting malformed duplicates."""

    try:
        year = int(str(year_text))
        value = Decimal(str(value_text).replace(",", "").strip())
    except (ValueError, ArithmeticError) as error:
        raise RetrievalError(
            f"Invalid {source_name} observation for {series_id}: "
            f"year={year_text!r}, value={value_text!r}"
        ) from error

    existing = observations[series_id].get(year)
    if existing is not None and existing != value:
        raise RetrievalError(
            f"Conflicting {source_name} M13 observations for {series_id}, {year}"
        )
    observations[series_id][year] = value


def validate_observations(
    observations: Mapping[str, Mapping[int, Decimal]], source_name: str
) -> None:
    """Require complete, matching, continuous annual records for every series."""

    missing_series = [series_id for series_id in SERIES_IDS if not observations[series_id]]
    if missing_series:
        raise RetrievalError(
            f"{source_name} has no M13 observations for: "
            + ", ".join(missing_series)
        )

    year_sets = {series_id: set(observations[series_id]) for series_id in SERIES_IDS}
    all_years = set().union(*year_sets.values())
    incomplete = {
        year: [series_id for series_id in SERIES_IDS if year not in year_sets[series_id]]
        for year in sorted(all_years)
        if any(year not in year_sets[series_id] for series_id in SERIES_IDS)
    }
    if incomplete:
        details = "; ".join(
            f"{year}: {', '.join(series_ids)}"
            for year, series_ids in list(incomplete.items())[:10]
        )
        raise RetrievalError(
            f"{source_name} is missing required annual observations ({details})"
        )

    first_year, last_year = min(all_years), max(all_years)
    missing_years = sorted(set(range(first_year, last_year + 1)) - all_years)
    if missing_years:
        raise RetrievalError(
            f"{source_name} annual observations have gaps: "
            + ", ".join(map(str, missing_years))
        )


def retrieve_bls_data() -> Tuple[Dict[str, Dict[int, Decimal]], str]:
    """Prefer the API, falling back to official bulk data after any API failure."""

    try:
        return fetch_from_api(), "api"
    except RetrievalError as api_error:
        print(
            f"BLS API unavailable or invalid: {api_error}\n"
            "Trying the official BLS LN bulk-data fallback...",
            file=sys.stderr,
        )
        try:
            return fetch_from_bulk_data(), "bulk"
        except RetrievalError as bulk_error:
            raise RetrievalError(
                "Both official retrieval methods failed. "
                f"API error: {api_error}. Bulk-data error: {bulk_error}"
            ) from bulk_error


def calculate_rows(
    observations: Mapping[str, Mapping[int, Decimal]], source_method: str
) -> list[Dict[str, object]]:
    """Calculate one audited annual indicator row for every matched year."""

    retrieval_date = date.today().isoformat()
    years = sorted(observations[ENROLLED_SERIES])
    rows: list[Dict[str, object]] = []

    for year in years:
        enrolled = observations[ENROLLED_SERIES][year]
        not_enrolled = observations[NOT_ENROLLED_SERIES][year]
        employed_not_enrolled = observations[EMPLOYED_NOT_ENROLLED_SERIES][year]
        denominator = enrolled + not_enrolled
        if denominator <= 0:
            raise RuntimeError(f"Invalid non-positive population denominator for {year}")
        if employed_not_enrolled > not_enrolled:
            raise RuntimeError(
                f"Employed not-enrolled population exceeds not-enrolled population for {year}"
            )

        calculated = (
            Decimal("100") * (not_enrolled - employed_not_enrolled) / denominator
        ).quantize(ONE_DECIMAL, rounding=ROUND_HALF_UP)
        rows.append(
            {
                "year": year,
                "enrolled_population_thousands": decimal_text(enrolled),
                "not_enrolled_population_thousands": decimal_text(not_enrolled),
                "employed_not_enrolled_thousands": decimal_text(
                    employed_not_enrolled
                ),
                "calculated_value": decimal_text(calculated),
                "source_method": source_method,
                "retrieval_date": retrieval_date,
            }
        )

    return rows


def decimal_text(value: Decimal) -> str:
    """Write ordinary decimal notation without scientific notation."""

    return format(value, "f")


def read_archived_values() -> Dict[int, Decimal]:
    """Read canonical legacy values directly from sdg-master.zip in SDGs.tar."""

    try:
        with tarfile.open(ARCHIVE_PATH, mode="r:*") as outer_archive:
            member = outer_archive.getmember(CANONICAL_ZIP_MEMBER)
            archived_zip = outer_archive.extractfile(member)
            if archived_zip is None:
                raise RuntimeError(f"Could not read {CANONICAL_ZIP_MEMBER}")
            zip_bytes = io.BytesIO(archived_zip.read())

        with zipfile.ZipFile(zip_bytes) as canonical_archive:
            csv_text = canonical_archive.read(CANONICAL_DATA_PATH).decode("utf-8-sig")
    except (KeyError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise RuntimeError(
            "Could not read the archived canonical SDG 8.6.1 CSV"
        ) from error

    archived: Dict[int, Decimal] = {}
    for row in csv.DictReader(io.StringIO(csv_text, newline="")):
        try:
            year = int(row["Year"])
            value = Decimal(row["Value"]).quantize(
                ONE_DECIMAL, rounding=ROUND_HALF_UP
            )
        except (KeyError, ValueError, ArithmeticError) as error:
            raise RuntimeError(f"Invalid archived SDG row: {row}") from error
        if year in archived:
            raise RuntimeError(f"Duplicate archived SDG year: {year}")
        archived[year] = value
    return archived


def validate_against_archive(
    calculated_rows: Sequence[Mapping[str, object]],
    archived_values: Mapping[int, Decimal],
) -> Dict[str, object]:
    """Compare calculated and archived values for every overlapping year."""

    calculated = {
        int(row["year"]): Decimal(str(row["calculated_value"]))
        for row in calculated_rows
    }
    overlap = sorted(set(calculated) & set(archived_values))
    if not overlap:
        raise RuntimeError("No overlapping years exist for archive validation")

    differences = {
        year: abs(calculated[year] - archived_values[year]) for year in overlap
    }
    mismatches = [year for year in overlap if differences[year] != 0]
    return {
        "overlapping_years": len(overlap),
        "exact_matches": len(overlap) - len(mismatches),
        "maximum_absolute_difference": max(differences.values()),
        "mismatching_years": mismatches,
    }


def write_output_atomically(rows: Sequence[Mapping[str, object]]) -> None:
    """Replace the output only after retrieval, calculation, and validation succeed."""

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{OUTPUT_PATH.name}.",
            suffix=".tmp",
            dir=OUTPUT_PATH.parent,
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            writer = csv.DictWriter(output_file, fieldnames=OUTPUT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, OUTPUT_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def print_report(
    rows: Sequence[Mapping[str, object]],
    source_method: str,
    validation: Mapping[str, object],
) -> None:
    latest = rows[-1]
    print(f"Wrote {OUTPUT_PATH}")
    print("Retrieval succeeded: yes")
    print(f"Source method: {source_method}")
    print(f"Latest year: {latest['year']}")
    print(f"Latest calculated value: {latest['calculated_value']}")
    print("Archive validation:")
    print(f"  overlapping years: {validation['overlapping_years']}")
    print(f"  exact matches: {validation['exact_matches']}")
    print(
        "  maximum absolute difference: "
        f"{decimal_text(validation['maximum_absolute_difference'])}"
    )
    mismatches = validation["mismatching_years"]
    print(
        "  mismatching years: "
        + (", ".join(map(str, mismatches)) if mismatches else "none")
    )


def main() -> None:
    try:
        observations, source_method = retrieve_bls_data()
        rows = calculate_rows(observations, source_method)
        archived_values = read_archived_values()
        validation = validate_against_archive(rows, archived_values)
        write_output_atomically(rows)
        print_report(rows, source_method, validation)
    except (RetrievalError, RuntimeError) as error:
        print(f"Pipeline failed; existing output was not changed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
