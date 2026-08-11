#!/usr/bin/env python3
"""Fetch and calculate the legacy U.S. implementation of SDG 4.2.2.

The measure is the weighted percentage of 5-year-olds enrolled in organized
learning (prekindergarten, kindergarten, or first grade or higher) in the
October Current Population Survey School Enrollment Supplement.

The script prefers the Census Microdata API when CENSUS_API_KEY is configured.
It otherwise falls back to official compressed public-use microdata files.
Downloaded files are parsed in memory and are not permanently extracted.

Important historical details:
* Age is age at last birthday at the October CPS interview.
* 2000--2005 use the basic final person weight PWSSWGT.
* 2006 onward use the School Enrollment Supplement weight PWSUPWGT.
* The 2020 estimate may be affected by pandemic-era response and enrollment
  classification issues documented by the Census Bureau.

Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Dict, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sdg_pipeline.archive import ArchiveReadError, read_nested_zip_member
from sdg_pipeline.errors import RetrievalError
from sdg_pipeline.output import current_retrieval_date, write_csv_outputs_atomically
from sdg_pipeline.sources import census_cps


ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
CANONICAL_DATA_PATH = "sdg-master/data/indicator_4-2-2.csv"

NATIONAL_OUTPUT_PATH = PROJECT_ROOT / "data_processed" / "sdg_4_2_2.csv"
SEX_OUTPUT_PATH = PROJECT_ROOT / "data_processed" / "sdg_4_2_2_by_sex.csv"

DEFAULT_START_YEAR = 2018
DEFAULT_END_YEAR = 2024
API_FIRST_YEAR = census_cps.API_FIRST_YEAR
API_LAST_YEAR = census_cps.API_LAST_YEAR
HTTP_TIMEOUT_SECONDS = census_cps.HTTP_TIMEOUT_SECONDS
USER_AGENT = census_cps.USER_AGENT

# CPS person weights in these files have four implied decimal places. Keeping
# the raw integer weights makes the percentage calculation exact; output counts
# are divided by this scale to show estimated people rather than storage units.
WEIGHT_SCALE = census_cps.WEIGHT_SCALE
ORGANIZED_LEARNING_GRADES = range(1, 17)

NATIONAL_COLUMNS = [
    "year",
    "weighted_numerator",
    "weighted_denominator",
    "unweighted_numerator",
    "unweighted_denominator",
    "weight_variable",
    "calculated_value",
    "source_url",
    "retrieval_method",
    "retrieval_date",
]
SEX_COLUMNS = ["year", "sex", *NATIONAL_COLUMNS[1:]]


@dataclass(frozen=True)
class PersonRecord:
    """The five CPS fields needed for this indicator."""

    age: int
    enrollment: int
    grade: int
    sex: int
    raw_weight: int


@dataclass(frozen=True)
class GroupResult:
    """Weighted and unweighted results for one national or sex group."""

    raw_weighted_numerator: int
    raw_weighted_denominator: int
    unweighted_numerator: int
    unweighted_denominator: int
    calculated_fraction: Fraction


@dataclass(frozen=True)
class YearResult:
    """Calculated results and source provenance for one survey year."""

    year: int
    weight_variable: str
    source_url: str
    retrieval_method: str
    national: GroupResult
    male: GroupResult
    female: GroupResult


@dataclass(frozen=True)
class ArchivedValue:
    """One archived value plus its displayed decimal precision."""

    value: Decimal
    decimal_places: int


FieldPosition = census_cps.FieldPosition
FixedWidthLayout = census_cps.FixedWidthLayout
DownloadConfig = census_cps.DownloadConfig
LAYOUT_2018_2024 = census_cps.LAYOUT_2018_2024
DOWNLOAD_CONFIGS = census_cps.DOWNLOAD_CONFIGS
REQUIRED_PERSON_VARIABLES = ("PRTAGE", "PESCH35", "PECHGRDE", "PESEX")


def census_download_url(year: int) -> str:
    """Compatibility wrapper for the connector's official download URL."""

    return census_cps.census_download_url(year)


def weight_variable_for_year(year: int) -> str:
    """Compatibility wrapper for the historical CPS weight transition."""

    return census_cps.weight_variable_for_year(year)


def api_dataset_url(year: int) -> str:
    """Compatibility wrapper for the public Census dataset landing page."""

    return census_cps.api_dataset_url(year)


def request_bytes(
    request: urllib.request.Request, display_url: str
) -> tuple[bytes, str]:
    """Read one HTTP response without exposing an API key in errors."""

    return census_cps.request_bytes(request, display_url)


def parse_integer(value: object, variable: str, year: int) -> int:
    """Compatibility wrapper for validated CPS integer parsing."""

    return census_cps.parse_integer(value, variable, year)


def person_from_mapping(
    row: Mapping[str, object], year: int, weight_variable: str
) -> PersonRecord:
    """Translate a generic connector observation to this indicator's record."""

    variables = (*REQUIRED_PERSON_VARIABLES, weight_variable)
    observation = census_cps.observation_from_mapping(row, year, variables)
    return person_from_observation(observation, weight_variable)


def person_from_observation(
    observation: census_cps.CpsObservation, weight_variable: str
) -> PersonRecord:
    """Translate named CPS variables without applying indicator calculations."""

    return PersonRecord(
        age=observation.value("PRTAGE"),
        enrollment=observation.value("PESCH35"),
        grade=observation.value("PECHGRDE"),
        sex=observation.value("PESEX"),
        raw_weight=observation.value(weight_variable),
    )


def fetch_from_api(year: int, api_key: str) -> list[PersonRecord]:
    """Fetch the requested age-5 fields through the Census connector."""

    weight_variable = weight_variable_for_year(year)
    variables = (*REQUIRED_PERSON_VARIABLES, weight_variable)
    observations = census_cps.fetch_from_api(
        year,
        api_key,
        variables,
        query_filters={"PRTAGE": 5},
        request_executor=request_bytes,
    )
    return [
        person_from_observation(observation, weight_variable)
        for observation in observations
    ]


def open_downloaded_records(
    body: bytes, config: DownloadConfig, year: int
):
    """Compatibility wrapper for ZIP/gzip record iteration."""

    return census_cps.open_downloaded_records(body, config, year)


def parse_fixed_width_record(
    raw_line: bytes, layout: FixedWidthLayout, year: int, weight_variable: str
) -> PersonRecord | None:
    """Translate one connector-parsed fixed-width record for this indicator."""

    variables = (*REQUIRED_PERSON_VARIABLES, weight_variable)
    observation = census_cps.parse_fixed_width_record(
        raw_line,
        layout,
        year,
        variables,
        record_filters={"PRTAGE": 5},
    )
    if observation is None:
        return None
    return person_from_observation(observation, weight_variable)


def fetch_from_download(year: int) -> tuple[list[PersonRecord], str]:
    """Fetch this indicator's fields from official public-use microdata."""

    weight_variable = weight_variable_for_year(year)
    variables = (*REQUIRED_PERSON_VARIABLES, weight_variable)
    observations, source_url = census_cps.fetch_from_download(
        year,
        variables,
        download_configs=DOWNLOAD_CONFIGS,
        record_filters={"PRTAGE": 5},
        request_executor=request_bytes,
        records_description="5-year-old records",
    )
    return (
        [
            person_from_observation(observation, weight_variable)
            for observation in observations
        ],
        source_url,
    )


def retrieve_year(
    year: int, api_key: str | None
) -> tuple[list[PersonRecord], str, str]:
    """Prefer the API when configured, then use the official download fallback."""

    weight_variable = weight_variable_for_year(year)
    variables = (*REQUIRED_PERSON_VARIABLES, weight_variable)
    result = census_cps.retrieve_year(
        year,
        variables,
        api_key=api_key,
        query_filters={"PRTAGE": 5},
        record_filters={"PRTAGE": 5},
        download_configs=DOWNLOAD_CONFIGS,
        api_fetcher=lambda: fetch_from_api(year, api_key or ""),
        download_fetcher=lambda: fetch_from_download(year),
        warning_handler=lambda warning: print(warning, file=sys.stderr),
    )
    return result.observations, result.retrieval_method, result.source_url


def calculate_group(
    records: Sequence[PersonRecord], label: str, year: int
) -> GroupResult:
    """Calculate one weighted group, requiring a nonempty positive denominator."""

    denominator_records = [record for record in records if record.raw_weight > 0]
    numerator_records = [
        record for record in denominator_records if record.enrollment == 1
    ]
    raw_denominator = sum(record.raw_weight for record in denominator_records)
    raw_numerator = sum(record.raw_weight for record in numerator_records)
    if not denominator_records or raw_denominator <= 0:
        raise RuntimeError(f"{year} {label} has no valid positive-weight records")
    return GroupResult(
        raw_weighted_numerator=raw_numerator,
        raw_weighted_denominator=raw_denominator,
        unweighted_numerator=len(numerator_records),
        unweighted_denominator=len(denominator_records),
        calculated_fraction=Fraction(100 * raw_numerator, raw_denominator),
    )


def calculate_year(
    year: int,
    records: Sequence[PersonRecord],
    retrieval_method: str,
    source_url: str,
) -> YearResult:
    """Validate person codes and calculate national, male, and female values."""

    if any(record.age != 5 for record in records):
        raise RuntimeError(f"{year} retrieval included a person who is not age 5")

    invalid_enrollment = sorted(
        {record.enrollment for record in records if record.enrollment not in {1, 2}}
    )
    if invalid_enrollment:
        raise RuntimeError(
            f"{year} has invalid PESCH35 codes for age-5 records: "
            + ", ".join(map(str, invalid_enrollment))
        )

    invalid_grades = sorted(
        {
            record.grade
            for record in records
            if record.enrollment == 1
            and record.grade not in ORGANIZED_LEARNING_GRADES
        }
    )
    if invalid_grades:
        raise RuntimeError(
            f"{year} has enrolled age-5 records outside the organized-learning "
            "grade range: "
            + ", ".join(map(str, invalid_grades))
        )

    invalid_sex = sorted({record.sex for record in records if record.sex not in {1, 2}})
    if invalid_sex:
        raise RuntimeError(
            f"{year} has invalid PESEX codes: " + ", ".join(map(str, invalid_sex))
        )

    male_records = [record for record in records if record.sex == 1]
    female_records = [record for record in records if record.sex == 2]
    national = calculate_group(records, "national", year)
    male = calculate_group(male_records, "male", year)
    female = calculate_group(female_records, "female", year)

    if (
        male.raw_weighted_denominator + female.raw_weighted_denominator
        != national.raw_weighted_denominator
        or male.raw_weighted_numerator + female.raw_weighted_numerator
        != national.raw_weighted_numerator
    ):
        raise RuntimeError(f"{year} male and female totals do not reconcile nationally")

    return YearResult(
        year=year,
        weight_variable=weight_variable_for_year(year),
        source_url=source_url,
        retrieval_method=retrieval_method,
        national=national,
        male=male,
        female=female,
    )


def fraction_to_decimal(value: Fraction) -> Decimal:
    """Convert an exact fraction to a high-precision Decimal for output."""

    with localcontext() as context:
        context.prec = 50
        return Decimal(value.numerator) / Decimal(value.denominator)


def decimal_text(value: Decimal) -> str:
    """Write ordinary decimal notation, trimming only insignificant zeros."""

    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def weighted_population_text(raw_weight_sum: int) -> str:
    """Write a raw four-implied-decimal CPS weight sum as estimated people."""

    value = Decimal(raw_weight_sum) / Decimal(WEIGHT_SCALE)
    return format(value, ".4f")


def group_output_fields(group: GroupResult) -> Dict[str, object]:
    """Return the shared audit columns for one calculated group."""

    return {
        "weighted_numerator": weighted_population_text(
            group.raw_weighted_numerator
        ),
        "weighted_denominator": weighted_population_text(
            group.raw_weighted_denominator
        ),
        "unweighted_numerator": group.unweighted_numerator,
        "unweighted_denominator": group.unweighted_denominator,
        "calculated_value": decimal_text(
            fraction_to_decimal(group.calculated_fraction)
        ),
    }


def build_output_rows(
    results: Sequence[YearResult], retrieval_date: str
) -> tuple[list[Dict[str, object]], list[Dict[str, object]]]:
    """Build national and sex-disaggregated output rows."""

    national_rows: list[Dict[str, object]] = []
    sex_rows: list[Dict[str, object]] = []
    for result in results:
        shared = {
            "year": result.year,
            "weight_variable": result.weight_variable,
            "source_url": result.source_url,
            "retrieval_method": result.retrieval_method,
            "retrieval_date": retrieval_date,
        }
        national_rows.append({**shared, **group_output_fields(result.national)})
        for sex, group in (("Male", result.male), ("Female", result.female)):
            sex_rows.append(
                {**shared, "sex": sex, **group_output_fields(group)}
            )
    return national_rows, sex_rows


def read_archived_values() -> Dict[tuple[int, str], ArchivedValue]:
    """Read national and sex values directly from sdg-master.zip in SDGs.tar."""

    try:
        csv_text = read_nested_zip_member(
            ARCHIVE_PATH, CANONICAL_ZIP_MEMBER, CANONICAL_DATA_PATH
        ).decode("utf-8-sig")
    except (ArchiveReadError, UnicodeDecodeError) as error:
        raise RuntimeError(
            "Could not read the archived canonical SDG 4.2.2 CSV"
        ) from error

    archived: Dict[tuple[int, str], ArchivedValue] = {}
    for row in csv.DictReader(io.StringIO(csv_text, newline="")):
        if (row.get("Income") or "").strip():
            continue
        sex = (row.get("Sex") or "").strip() or "National"
        if sex not in {"National", "Male", "Female"}:
            continue
        value_text = (row.get("Value") or "").strip()
        try:
            year = int(row["Year"])
            value = Decimal(value_text)
        except (KeyError, ValueError, ArithmeticError) as error:
            raise RuntimeError(f"Invalid archived SDG row: {row}") from error
        decimal_places = len(value_text.partition(".")[2]) if "." in value_text else 0
        key = (year, sex)
        if key in archived:
            raise RuntimeError(f"Duplicate archived SDG row: {year} {sex}")
        archived[key] = ArchivedValue(value, decimal_places)
    return archived


def compare_at_archived_precision(
    calculated: Fraction, archived: ArchivedValue
) -> Decimal:
    """Return absolute difference after matching the archive's stored precision."""

    quantizer = Decimal(1).scaleb(-archived.decimal_places)
    rounded = fraction_to_decimal(calculated).quantize(
        quantizer, rounding=ROUND_HALF_UP
    )
    return abs(rounded - archived.value)


def validate_results(
    results: Sequence[YearResult], archived: Mapping[tuple[int, str], ArchivedValue]
) -> Dict[str, object]:
    """Validate every available national and sex result against the archive."""

    national_differences: Dict[int, Decimal] = {}
    sex_differences: Dict[tuple[int, str], Decimal] = {}
    for result in results:
        national_key = (result.year, "National")
        if national_key in archived:
            national_differences[result.year] = compare_at_archived_precision(
                result.national.calculated_fraction, archived[national_key]
            )
        for sex, group in (("Male", result.male), ("Female", result.female)):
            key = (result.year, sex)
            if key in archived:
                sex_differences[key] = compare_at_archived_precision(
                    group.calculated_fraction, archived[key]
                )

    if not national_differences:
        raise RuntimeError("No overlapping national years exist for archive validation")

    national_mismatches = [
        year for year, difference in national_differences.items() if difference != 0
    ]
    sex_mismatches = [
        key for key, difference in sex_differences.items() if difference != 0
    ]
    return {
        "national_overlaps": len(national_differences),
        "national_exact_matches": len(national_differences) - len(national_mismatches),
        "national_maximum_difference": max(national_differences.values()),
        "national_mismatches": national_mismatches,
        "sex_overlaps": len(sex_differences),
        "sex_exact_matches": len(sex_differences) - len(sex_mismatches),
        "sex_maximum_difference": max(sex_differences.values(), default=Decimal(0)),
        "sex_mismatches": sex_mismatches,
    }


def write_outputs_atomically(
    national_rows: Sequence[Mapping[str, object]],
    sex_rows: Sequence[Mapping[str, object]],
) -> None:
    """Replace outputs only after both complete temporary files are ready."""

    write_csv_outputs_atomically(
        [
            (NATIONAL_OUTPUT_PATH, NATIONAL_COLUMNS, national_rows),
            (SEX_OUTPUT_PATH, SEX_COLUMNS, sex_rows),
        ]
    )


def print_report(
    results: Sequence[YearResult], validation: Mapping[str, object]
) -> None:
    """Print a concise retrieval and archive-validation report."""

    latest = results[-1]
    methods = sorted({result.retrieval_method for result in results})
    years = [result.year for result in results]
    print(f"Wrote {NATIONAL_OUTPUT_PATH}")
    print(f"Wrote {SEX_OUTPUT_PATH}")
    print("Retrieval succeeded: yes")
    print("Retrieval method(s): " + ", ".join(methods))
    print("Years successfully retrieved: " + ", ".join(map(str, years)))
    print(f"Latest year: {latest.year}")
    print(
        "Latest calculated national value: "
        + decimal_text(fraction_to_decimal(latest.national.calculated_fraction))
    )
    print("National archive validation:")
    print(f"  overlapping years: {validation['national_overlaps']}")
    print(f"  exact matches: {validation['national_exact_matches']}")
    print(
        "  maximum absolute difference: "
        f"{decimal_text(validation['national_maximum_difference'])}"
    )
    national_mismatches = validation["national_mismatches"]
    print(
        "  mismatching years: "
        + (
            ", ".join(map(str, national_mismatches))
            if national_mismatches
            else "none"
        )
    )
    print("Sex archive validation:")
    print(f"  overlapping rows: {validation['sex_overlaps']}")
    print(f"  exact matches: {validation['sex_exact_matches']}")
    print(
        "  maximum absolute difference: "
        f"{decimal_text(validation['sex_maximum_difference'])}"
    )
    sex_mismatches = validation["sex_mismatches"]
    print(
        "  mismatching rows: "
        + (
            ", ".join(f"{year} {sex}" for year, sex in sex_mismatches)
            if sex_mismatches
            else "none"
        )
    )
    if 2020 in years:
        print(
            "Warning: Census notes that pandemic-era response and enrollment "
            "classification issues may affect the 2020 estimate."
        )


def parse_arguments() -> argparse.Namespace:
    """Parse an inclusive survey-year range."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR)
    arguments = parser.parse_args()
    if arguments.start_year > arguments.end_year:
        parser.error("--start-year cannot be later than --end-year")
    return arguments


def main() -> None:
    arguments = parse_arguments()
    api_key = census_cps.configured_api_key()
    try:
        retrieval_date = current_retrieval_date()
        results = []
        for year in range(arguments.start_year, arguments.end_year + 1):
            records, method, source_url = retrieve_year(year, api_key)
            results.append(calculate_year(year, records, method, source_url))

        archived = read_archived_values()
        validation = validate_results(results, archived)
        national_rows, sex_rows = build_output_rows(
            results, retrieval_date=retrieval_date
        )
        write_outputs_atomically(national_rows, sex_rows)
        print_report(results, validation)
    except (RetrievalError, RuntimeError, OSError) as error:
        print(
            f"Pipeline failed; existing outputs were not changed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
