"""Statistical interpretation for the U.S. implementation of SDG 8.5.2.

BLS publishes these unemployment rates directly.  This module therefore maps
verified LABSTAT series to understandable disaggregations and preserves the
published values; it does not reconstruct rates from rounded levels.

The BLS source connector remains responsible for HTTP retrieval, API response
validation, bulk fallback, and selection of annual ``A01`` observations.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from ..archive import ArchiveReadError, read_nested_zip_member
from ..standardized import (
    ARCHIVE_MATCHED,
    ARCHIVE_MISMATCH,
    CURRENT_METHODOLOGY_VERIFIED,
    StandardizedObservation,
    serialize_disaggregation,
)


INDICATOR_ID = "8.5.2"
INDICATOR_TITLE = "Unemployment rate, by sex, age and persons with disabilities"
REQUIRED_PERIOD = "A01"
FIRST_SOURCE_YEAR = 2009
METHODOLOGY_VARIANT = "us_cps_unemployment_rate_age16_plus"
SOURCE_DATASET = "Current Population Survey / LABSTAT"
AGE_COVERAGE_WARNING = (
    "The U.S. Current Population Survey labor-force universe begins at age 16. "
    "The global SDG framework generally begins at age 15; this is a U.S. "
    "age-coverage qualification, not a calculation error."
)
YEAR_2025_WARNING = (
    "BLS 2025 CPS annual estimates are 11-month averages that exclude October "
    "because data were not collected during the federal government shutdown; "
    "they are not strictly comparable with full-year annual averages."
)

CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
CANONICAL_DATA_PATH = "sdg-master/data/indicator_8-5-2.csv"


@dataclass(frozen=True)
class SeriesDefinition:
    """Meaning assigned to one verified published annual BLS series."""

    series_id: str
    disaggregation: Mapping[str, str]
    headline: bool = False


# These four series match archive rows whose age is "16 +".  Age is implicit
# in the BLS series universe, matching the requested compact card labels.
HEADLINE_SERIES = (
    SeriesDefinition(
        "LNU04075630", {"sex": "Male", "disability": "With disability"}, True
    ),
    SeriesDefinition(
        "LNU04075704", {"sex": "Female", "disability": "With disability"}, True
    ),
    SeriesDefinition(
        "LNU04075409", {"sex": "Male", "disability": "No disability"}, True
    ),
    SeriesDefinition(
        "LNU04075483", {"sex": "Female", "disability": "No disability"}, True
    ),
)


def _age_pair(
    age: str, no_disability_series: str, disability_series: str
) -> tuple[SeriesDefinition, SeriesDefinition]:
    """Build the two published disability-status series for one age group."""

    return (
        SeriesDefinition(
            no_disability_series,
            {"age": age, "disability": "No disability"},
        ),
        SeriesDefinition(
            disability_series,
            {"age": age, "disability": "With disability"},
        ),
    )


# BLS publishes these as age totals, not as sex-by-age intersections.  The
# pipeline must not invent intersections that are absent from LABSTAT.
AGE_SERIES = (
    *_age_pair("16-19", "LNU04074596", "LNU04074600"),
    *_age_pair("20-24", "LNU04075349", "LNU04075570"),
    *_age_pair("25+", "LNU04075354", "LNU04075575"),
    *_age_pair("25-34", "LNU04075359", "LNU04075580"),
    *_age_pair("35-44", "LNU04075364", "LNU04075585"),
    *_age_pair("45-54", "LNU04075369", "LNU04075590"),
    *_age_pair("55-64", "LNU04075374", "LNU04075595"),
    *_age_pair("16-64", "LNU04076935", "LNU04076950"),
)

SERIES_DEFINITIONS = HEADLINE_SERIES + AGE_SERIES
HEADLINE_SERIES_IDS = tuple(item.series_id for item in HEADLINE_SERIES)
OPTIONAL_AGE_SERIES_IDS = tuple(item.series_id for item in AGE_SERIES)
SERIES_IDS = tuple(item.series_id for item in SERIES_DEFINITIONS)
SERIES_BY_ID = {item.series_id: item for item in SERIES_DEFINITIONS}


@dataclass(frozen=True)
class ArchivedData:
    """Directly comparable archive values plus the full archive row count."""

    comparable_values: Mapping[tuple[int, str, str], Decimal]
    total_rows: int


def decimal_text(value: Decimal) -> str:
    """Preserve BLS's ordinary decimal representation without extra rounding."""

    return format(value, "f")


def data_warning(year: int) -> str:
    """Return qualifications relevant to one published annual observation."""

    if year == 2025:
        return f"{AGE_COVERAGE_WARNING} {YEAR_2025_WARNING}"
    return AGE_COVERAGE_WARNING


def validate_observations(
    observations: Mapping[str, Mapping[int, Decimal]],
    required_period: str = REQUIRED_PERIOD,
) -> tuple[int, ...]:
    """Validate required headline series and any available optional age series.

    A completely absent age series is allowed because age detail is optional.
    A present age series must cover the same years as the headline series so a
    partial or malformed source response cannot silently enter the output.
    """

    if required_period != REQUIRED_PERIOD:
        raise RuntimeError(
            f"SDG 8.5.2 requires BLS {REQUIRED_PERIOD} annual observations; "
            f"received {required_period!r}"
        )

    missing_headlines = [
        series_id
        for series_id in HEADLINE_SERIES_IDS
        if not observations.get(series_id)
    ]
    if missing_headlines:
        raise RuntimeError(
            "SDG 8.5.2 is missing required headline BLS series: "
            + ", ".join(missing_headlines)
        )

    headline_years = set(observations[HEADLINE_SERIES_IDS[0]])
    for series_id in HEADLINE_SERIES_IDS[1:]:
        if set(observations[series_id]) != headline_years:
            raise RuntimeError("SDG 8.5.2 headline series do not contain matching years")
    if not headline_years:
        raise RuntimeError("SDG 8.5.2 contains no annual observations")

    for series_id in OPTIONAL_AGE_SERIES_IDS:
        series_values = observations.get(series_id)
        if not series_values:
            continue
        if set(series_values) != headline_years:
            raise RuntimeError(
                f"Optional age series {series_id} does not match headline years"
            )

    for series_id in SERIES_IDS:
        for year, value in observations.get(series_id, {}).items():
            if not value.is_finite() or value < 0 or value > 100:
                raise RuntimeError(
                    f"Invalid BLS unemployment rate for {series_id}, {year}: {value}"
                )
    return tuple(sorted(headline_years))


def missing_optional_series(
    observations: Mapping[str, Mapping[int, Decimal]],
) -> tuple[str, ...]:
    """List absent age series without treating them as a pipeline failure."""

    return tuple(
        series_id
        for series_id in OPTIONAL_AGE_SERIES_IDS
        if not observations.get(series_id)
    )


def parse_archived_values(csv_text: str) -> ArchivedData:
    """Read archive rows and retain only exact headline-series counterparts."""

    comparable: dict[tuple[int, str, str], Decimal] = {}
    total_rows = 0
    sex_labels = {"Men": "Male", "Women": "Female"}
    disability_labels = {
        "Persons with a disability": "With disability",
        "Persons with no disability": "No disability",
    }
    for row in csv.DictReader(io.StringIO(csv_text, newline="")):
        total_rows += 1
        if row.get("Age", "").strip() != "16 +":
            continue
        try:
            year = int(row["Year"])
            sex = sex_labels[row["Sex"].strip()]
            disability = disability_labels[row["Disability Status"].strip()]
            value = Decimal(row["Value"].strip())
        except (KeyError, ValueError, ArithmeticError) as error:
            raise RuntimeError(f"Invalid archived SDG 8.5.2 row: {row}") from error
        key = (year, sex, disability)
        if key in comparable:
            raise RuntimeError(f"Duplicate comparable archived SDG 8.5.2 row: {key}")
        comparable[key] = value

    if not comparable:
        raise RuntimeError("Archived SDG 8.5.2 CSV has no comparable headline rows")
    return ArchivedData(comparable, total_rows)


def read_archived_values(archive_path: Path) -> ArchivedData:
    """Read the canonical SDG 8.5.2 CSV without extracting the archive."""

    try:
        csv_text = read_nested_zip_member(
            archive_path, CANONICAL_ZIP_MEMBER, CANONICAL_DATA_PATH
        ).decode("utf-8-sig")
    except (ArchiveReadError, UnicodeDecodeError) as error:
        raise RuntimeError(
            "Could not read the archived canonical SDG 8.5.2 CSV"
        ) from error
    return parse_archived_values(csv_text)


def _headline_values(
    observations: Mapping[str, Mapping[int, Decimal]],
) -> dict[tuple[int, str, str], Decimal]:
    values: dict[tuple[int, str, str], Decimal] = {}
    for definition in HEADLINE_SERIES:
        sex = definition.disaggregation["sex"]
        disability = definition.disaggregation["disability"]
        for year, value in observations[definition.series_id].items():
            values[(year, sex, disability)] = value
    return values


def validate_against_archive(
    observations: Mapping[str, Mapping[int, Decimal]], archived: ArchivedData
) -> dict[str, object]:
    """Compare every direct headline-series overlap with the canonical archive."""

    validate_observations(observations)
    current = _headline_values(observations)
    overlap = sorted(set(current) & set(archived.comparable_values))
    if not overlap:
        raise RuntimeError("No directly comparable rows exist for archive validation")

    differences = {
        key: abs(current[key] - archived.comparable_values[key]) for key in overlap
    }
    mismatches = [key for key in overlap if differences[key] != 0]
    return {
        "overlapping_rows": len(overlap),
        "exact_matches": len(overlap) - len(mismatches),
        "maximum_absolute_difference": max(differences.values()),
        "mismatching_rows": mismatches,
        "non_comparable_archived_rows": (
            archived.total_rows - len(archived.comparable_values)
        ),
    }


def build_standardized_observations(
    observations: Mapping[str, Mapping[int, Decimal]],
    archived: ArchivedData,
    source_organization: str,
    source_url: str,
    retrieval_method: str,
    retrieval_date: str,
    required_period: str = REQUIRED_PERIOD,
) -> list[StandardizedObservation]:
    """Map published BLS rates into the common 15-column output schema."""

    validate_observations(observations, required_period)
    standardized: list[StandardizedObservation] = []
    identities: set[tuple[int, str]] = set()

    for definition in SERIES_DEFINITIONS:
        for year, value in sorted(observations.get(definition.series_id, {}).items()):
            identity = (year, serialize_disaggregation(definition.disaggregation))
            if identity in identities:
                raise RuntimeError(
                    f"Duplicate standardized SDG 8.5.2 observation: {identity}"
                )
            identities.add(identity)

            validation_status = CURRENT_METHODOLOGY_VERIFIED
            if definition.headline:
                key = (
                    year,
                    definition.disaggregation["sex"],
                    definition.disaggregation["disability"],
                )
                archived_value = archived.comparable_values.get(key)
                if archived_value is not None:
                    validation_status = (
                        ARCHIVE_MATCHED if value == archived_value else ARCHIVE_MISMATCH
                    )

            standardized.append(
                StandardizedObservation(
                    indicator_id=INDICATOR_ID,
                    indicator_title=INDICATOR_TITLE,
                    year=year,
                    value=decimal_text(value),
                    unit="percent",
                    geography="United States",
                    disaggregation=definition.disaggregation,
                    source_organization=source_organization,
                    source_dataset=SOURCE_DATASET,
                    source_url=source_url,
                    retrieval_method=retrieval_method,
                    retrieval_date=retrieval_date,
                    methodology_variant=METHODOLOGY_VARIANT,
                    validation_status=validation_status,
                    data_warning=data_warning(year),
                )
            )

    return sorted(
        standardized,
        key=lambda item: (
            item.year,
            serialize_disaggregation(item.disaggregation),
        ),
    )


def latest_headline_values(
    observations: Mapping[str, Mapping[int, Decimal]],
) -> tuple[int, dict[str, Decimal]]:
    """Return the newest four card values after normal indicator validation."""

    years = validate_observations(observations)
    latest_year = years[-1]
    return latest_year, {
        f"{item.disaggregation['sex']} / {item.disaggregation['disability']}": (
            observations[item.series_id][latest_year]
        )
        for item in HEADLINE_SERIES
    }
