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
import os
import sys
import urllib.request
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Mapping, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sdg_pipeline.archive import ArchiveReadError, read_nested_zip_member
from sdg_pipeline.errors import RetrievalError
from sdg_pipeline.output import current_retrieval_date, write_csv_atomically
from sdg_pipeline.sources import bls


ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
CANONICAL_DATA_PATH = "sdg-master/data/indicator_8-6-1.csv"
OUTPUT_PATH = PROJECT_ROOT / "data_processed" / "sdg_8_6_1.csv"

BLS_API_URL = bls.BLS_API_URL
BLS_BULK_DATA_URL = bls.BLS_BULK_DATA_URL

ENROLLED_SERIES = "LNU00022967"
NOT_ENROLLED_SERIES = "LNU00023016"
EMPLOYED_NOT_ENROLLED_SERIES = "LNU02023016"
SERIES_IDS = (
    ENROLLED_SERIES,
    NOT_ENROLLED_SERIES,
    EMPLOYED_NOT_ENROLLED_SERIES,
)

FIRST_BLS_YEAR = 1985
API_YEAR_CHUNK = bls.API_YEAR_CHUNK
HTTP_TIMEOUT_SECONDS = bls.HTTP_TIMEOUT_SECONDS
USER_AGENT = bls.USER_AGENT
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


def request_bytes(request: urllib.request.Request) -> Tuple[bytes, str]:
    """Return an HTTP response body and content type with a bounded timeout."""

    return bls.request_bytes(request)


def year_chunks(start_year: int, end_year: int):
    """Compatibility wrapper for the BLS connector's API-safe year chunks."""

    return bls.year_chunks(start_year, end_year, API_YEAR_CHUNK)


def fetch_from_api() -> Dict[str, Dict[int, Decimal]]:
    """Fetch this indicator's three M13 series through the BLS connector."""

    return bls.fetch_from_api(
        SERIES_IDS,
        FIRST_BLS_YEAR,
        date.today().year,
        "M13",
        request_executor=request_bytes,
        chunker=year_chunks,
    )


def fetch_from_bulk_data() -> Dict[str, Dict[int, Decimal]]:
    """Fetch this indicator's three M13 series from LABSTAT bulk data."""

    return bls.fetch_from_bulk_data(SERIES_IDS, "M13")


def add_observation(
    observations: Dict[str, Dict[int, Decimal]],
    series_id: str,
    year_text: object,
    value_text: object,
    source_name: str,
) -> None:
    """Compatibility wrapper for BLS observation parsing."""

    bls.add_observation(
        observations, series_id, year_text, value_text, source_name, "M13"
    )


def validate_observations(
    observations: Mapping[str, Mapping[int, Decimal]], source_name: str
) -> None:
    """Compatibility wrapper for BLS observation validation."""

    bls.validate_observations(observations, SERIES_IDS, source_name, "M13")


def retrieve_bls_data() -> Tuple[Dict[str, Dict[int, Decimal]], str]:
    """Prefer the API, falling back to official bulk data after any API failure."""

    result = bls.retrieve(
        SERIES_IDS,
        FIRST_BLS_YEAR,
        date.today().year,
        "M13",
        api_fetcher=fetch_from_api,
        bulk_fetcher=fetch_from_bulk_data,
        warning_handler=lambda warning: print(warning, file=sys.stderr),
    )
    return result.observations, result.retrieval_method


def calculate_rows(
    observations: Mapping[str, Mapping[int, Decimal]],
    source_method: str,
    retrieval_date: str | None = None,
) -> list[Dict[str, object]]:
    """Calculate one audited annual indicator row for every matched year."""

    retrieval_date = retrieval_date or current_retrieval_date()
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
        csv_text = read_nested_zip_member(
            ARCHIVE_PATH, CANONICAL_ZIP_MEMBER, CANONICAL_DATA_PATH
        ).decode("utf-8-sig")
    except (ArchiveReadError, UnicodeDecodeError) as error:
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

    write_csv_atomically(OUTPUT_PATH, OUTPUT_COLUMNS, rows)


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
        retrieval_date = current_retrieval_date()
        observations, source_method = retrieve_bls_data()
        rows = calculate_rows(observations, source_method, retrieval_date)
        archived_values = read_archived_values()
        validation = validate_against_archive(rows, archived_values)
        write_output_atomically(rows)
        print_report(rows, source_method, validation)
    except (RetrievalError, RuntimeError) as error:
        print(f"Pipeline failed; existing output was not changed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
