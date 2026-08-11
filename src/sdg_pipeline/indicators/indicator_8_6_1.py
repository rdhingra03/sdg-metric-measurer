"""Statistical definition for the legacy U.S. implementation of SDG 8.6.1.

This module deliberately knows what the three BLS series mean and how they
form the indicator. It does not know how BLS HTTP requests or bulk files work;
those responsibilities belong to :mod:`sdg_pipeline.sources.bls`.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Mapping, Sequence

from ..archive import ArchiveReadError, read_nested_zip_member
from ..output import current_retrieval_date


INDICATOR_ID = "8.6.1"
ENROLLED_SERIES = "LNU00022967"
NOT_ENROLLED_SERIES = "LNU00023016"
EMPLOYED_NOT_ENROLLED_SERIES = "LNU02023016"
SERIES_ROLES = {
    "enrolled_population": ENROLLED_SERIES,
    "not_enrolled_population": NOT_ENROLLED_SERIES,
    "employed_not_enrolled_population": EMPLOYED_NOT_ENROLLED_SERIES,
}
SERIES_IDS = tuple(SERIES_ROLES.values())
REQUIRED_PERIOD = "M13"
FIRST_SOURCE_YEAR = 1985
ONE_DECIMAL = Decimal("0.1")

CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
CANONICAL_DATA_PATH = "sdg-master/data/indicator_8-6-1.csv"

LEGACY_US_INTERPRETATION = (
    "Percentage of people ages 16-24 who are not enrolled in school and are "
    "not employed."
)
METHODOLOGY_WARNING = (
    "This is a legacy U.S.-adapted proxy covering ages 16-24 and school "
    "enrollment. The current global SDG 8.6.1 definition measures people "
    "ages 15-24 not in employment, education, or training (NEET)."
)


@dataclass(frozen=True)
class Calculation:
    """One full-precision calculation and its one-decimal presentation value."""

    unrounded_value: Decimal
    presented_value: Decimal


def decimal_text(value: Decimal) -> str:
    """Write ordinary decimal notation without scientific notation."""

    return format(value, "f")


def validate_observations(
    observations: Mapping[str, Mapping[int, Decimal]],
) -> list[int]:
    """Validate that the three semantic inputs are present and year-matched."""

    missing_series = [series_id for series_id in SERIES_IDS if series_id not in observations]
    if missing_series:
        raise RuntimeError(
            "SDG 8.6.1 is missing required BLS series: "
            + ", ".join(missing_series)
        )
    empty_series = [series_id for series_id in SERIES_IDS if not observations[series_id]]
    if empty_series:
        raise RuntimeError(
            "SDG 8.6.1 has no observations for required BLS series: "
            + ", ".join(empty_series)
        )

    year_sets = [set(observations[series_id]) for series_id in SERIES_IDS]
    if any(years != year_sets[0] for years in year_sets[1:]):
        raise RuntimeError("SDG 8.6.1 BLS series do not contain matching years")
    return sorted(year_sets[0])


def calculate_value(
    enrolled: Decimal,
    not_enrolled: Decimal,
    employed_not_enrolled: Decimal,
    year: int,
) -> Calculation:
    """Calculate the legacy proxy and round its presentation to one decimal."""

    denominator = enrolled + not_enrolled
    if denominator <= 0:
        raise RuntimeError(f"Invalid non-positive population denominator for {year}")
    if employed_not_enrolled > not_enrolled:
        raise RuntimeError(
            "Employed not-enrolled population exceeds not-enrolled population "
            f"for {year}"
        )

    unrounded = (
        Decimal("100") * (not_enrolled - employed_not_enrolled) / denominator
    )
    return Calculation(
        unrounded_value=unrounded,
        presented_value=unrounded.quantize(ONE_DECIMAL, rounding=ROUND_HALF_UP),
    )


def calculate(
    observations: Mapping[str, Mapping[int, Decimal]],
    source_method: str,
    retrieval_date: str | None = None,
) -> list[Dict[str, object]]:
    """Build the existing audited output rows from structured BLS observations."""

    years = validate_observations(observations)
    retrieval_date = retrieval_date or current_retrieval_date()
    rows: list[Dict[str, object]] = []
    for year in years:
        enrolled = observations[ENROLLED_SERIES][year]
        not_enrolled = observations[NOT_ENROLLED_SERIES][year]
        employed_not_enrolled = observations[EMPLOYED_NOT_ENROLLED_SERIES][year]
        calculation = calculate_value(
            enrolled, not_enrolled, employed_not_enrolled, year
        )
        rows.append(
            {
                "year": year,
                "enrolled_population_thousands": decimal_text(enrolled),
                "not_enrolled_population_thousands": decimal_text(not_enrolled),
                "employed_not_enrolled_thousands": decimal_text(
                    employed_not_enrolled
                ),
                "calculated_value": decimal_text(calculation.presented_value),
                "source_method": source_method,
                "retrieval_date": retrieval_date,
            }
        )
    return rows


def parse_archived_values(csv_text: str) -> Dict[int, Decimal]:
    """Parse canonical legacy values using the archive's one-decimal rule."""

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


def read_archived_values(archive_path: Path) -> Dict[int, Decimal]:
    """Read and parse this indicator's canonical CSV from the legacy archive."""

    try:
        csv_text = read_nested_zip_member(
            archive_path, CANONICAL_ZIP_MEMBER, CANONICAL_DATA_PATH
        ).decode("utf-8-sig")
    except (ArchiveReadError, UnicodeDecodeError) as error:
        raise RuntimeError(
            "Could not read the archived canonical SDG 8.6.1 CSV"
        ) from error
    return parse_archived_values(csv_text)


def validate_against_archive(
    calculated_rows: Sequence[Mapping[str, object]],
    archived_values: Mapping[int, Decimal],
) -> Dict[str, object]:
    """Compare presented calculations with every overlapping archived year."""

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
