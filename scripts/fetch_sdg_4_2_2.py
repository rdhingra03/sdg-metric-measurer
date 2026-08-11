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
import os
import sys
import urllib.request
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Dict, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sdg_pipeline.errors import RetrievalError
from sdg_pipeline.indicators import indicator_4_2_2 as indicator
from sdg_pipeline.output import current_retrieval_date, write_csv_outputs_atomically
from sdg_pipeline.sources import census_cps
from sdg_pipeline.standardized import write_standardized_csv


ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
CANONICAL_ZIP_MEMBER = indicator.CANONICAL_ZIP_MEMBER
CANONICAL_DATA_PATH = indicator.CANONICAL_DATA_PATH

NATIONAL_OUTPUT_PATH = PROJECT_ROOT / "data_processed" / "sdg_4_2_2.csv"
SEX_OUTPUT_PATH = PROJECT_ROOT / "data_processed" / "sdg_4_2_2_by_sex.csv"
STANDARDIZED_OUTPUT_PATH = (
    PROJECT_ROOT / "data_processed" / "standardized" / "sdg_4_2_2.csv"
)

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
ORGANIZED_LEARNING_GRADES = indicator.ORGANIZED_LEARNING_GRADES

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


PersonRecord = indicator.PersonRecord
GroupResult = indicator.GroupResult
YearResult = indicator.YearResult
ArchivedValue = indicator.ArchivedValue


FieldPosition = census_cps.FieldPosition
FixedWidthLayout = census_cps.FixedWidthLayout
DownloadConfig = census_cps.DownloadConfig
LAYOUT_2018_2024 = census_cps.LAYOUT_2018_2024
DOWNLOAD_CONFIGS = census_cps.DOWNLOAD_CONFIGS
REQUIRED_PERSON_VARIABLES = indicator.REQUIRED_PERSON_VARIABLES


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
    return indicator.person_from_observation(observation, weight_variable)


def person_from_observation(
    observation: census_cps.CpsObservation, weight_variable: str
) -> PersonRecord:
    """Compatibility wrapper for the indicator's CPS field mapping."""

    return indicator.person_from_observation(observation, weight_variable)


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
    return indicator.select_age_five(observations, weight_variable)


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
    return indicator.person_from_observation(observation, weight_variable)


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
        indicator.select_age_five(observations, weight_variable),
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
    """Compatibility wrapper for one weighted indicator group."""

    return indicator.calculate_group(records, label, year)


def calculate_year(
    year: int,
    records: Sequence[PersonRecord],
    retrieval_method: str,
    source_url: str,
) -> YearResult:
    """Compatibility wrapper for national and sex-disaggregated calculation."""

    return indicator.calculate(
        year,
        records,
        retrieval_method,
        source_url,
        weight_variable_for_year(year),
    )


def fraction_to_decimal(value: Fraction) -> Decimal:
    """Compatibility wrapper for exact high-precision conversion."""

    return indicator.fraction_to_decimal(value)


def decimal_text(value: Decimal) -> str:
    """Compatibility wrapper for indicator decimal formatting."""

    return indicator.decimal_text(value)


def weighted_population_text(raw_weight_sum: int) -> str:
    """Compatibility wrapper for implied-decimal CPS weight display."""

    return indicator.weighted_population_text(raw_weight_sum, WEIGHT_SCALE)


def group_output_fields(group: GroupResult) -> Dict[str, object]:
    """Compatibility wrapper for indicator audit fields."""

    return indicator.group_output_fields(group, WEIGHT_SCALE)


def build_output_rows(
    results: Sequence[YearResult], retrieval_date: str
) -> tuple[list[Dict[str, object]], list[Dict[str, object]]]:
    """Compatibility wrapper for existing processed output rows."""

    return indicator.build_output_rows(results, retrieval_date, WEIGHT_SCALE)


def read_archived_values() -> Dict[tuple[int, str], ArchivedValue]:
    """Compatibility wrapper for indicator-specific archive interpretation."""

    return indicator.read_archived_values(ARCHIVE_PATH)


def compare_at_archived_precision(
    calculated: Fraction, archived: ArchivedValue
) -> Decimal:
    """Compatibility wrapper for stored-precision comparison."""

    return indicator.compare_at_archived_precision(calculated, archived)


def validate_results(
    results: Sequence[YearResult], archived: Mapping[tuple[int, str], ArchivedValue]
) -> Dict[str, object]:
    """Compatibility wrapper for indicator-specific archive validation."""

    return indicator.validate_against_archive(results, archived)


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


def write_standardized_output(observations) -> None:
    """Atomically write national and sex observations in the common schema."""

    write_standardized_csv(STANDARDIZED_OUTPUT_PATH, observations)


def print_report(
    results: Sequence[YearResult], validation: Mapping[str, object]
) -> None:
    """Print a concise retrieval and archive-validation report."""

    latest = results[-1]
    methods = sorted({result.retrieval_method for result in results})
    years = [result.year for result in results]
    print(f"Wrote {NATIONAL_OUTPUT_PATH}")
    print(f"Wrote {SEX_OUTPUT_PATH}")
    print(f"Wrote {STANDARDIZED_OUTPUT_PATH}")
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
        print(f"Warning: {indicator.PANDEMIC_WARNING}")


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
        standardized_observations = indicator.build_standardized_observations(
            results,
            archived,
            retrieval_date,
            census_cps.CENSUS_SOURCE_ORGANIZATION,
            census_cps.CENSUS_SOURCE_DATASET,
        )
        write_outputs_atomically(national_rows, sex_rows)
        write_standardized_output(standardized_observations)
        print_report(results, validation)
    except (RetrievalError, RuntimeError, OSError) as error:
        print(
            f"Pipeline failed; existing outputs were not changed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
