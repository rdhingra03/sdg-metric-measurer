#!/usr/bin/env python3
"""Fetch the current U.S. implementation of SDG indicator 8.5.2.

The pipeline retrieves official, unadjusted BLS Current Population Survey
unemployment-rate series and selects annual ``A01`` observations.  BLS
publishes the rates directly, so this script preserves them instead of
reconstructing rates from rounded unemployment and labor-force levels.

The U.S. CPS labor-force universe begins at age 16, while the global SDG
framework generally begins at age 15.  The output documents this national
age-coverage qualification on every row.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sdg_pipeline.errors import RetrievalError
from sdg_pipeline.indicators import indicator_8_5_2 as indicator
from sdg_pipeline.sources import bls
from sdg_pipeline.standardized import write_standardized_csv


ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
OUTPUT_PATH = (
    PROJECT_ROOT / "data_processed" / "standardized" / "sdg_8_5_2.csv"
)


def retrieve_bls_data() -> bls.BlsResult:
    """Retrieve verified series, retaining headlines if optional ages disappear.

    The first request is the normal efficient path. If BLS stops publishing an
    optional age series, the connector correctly rejects that incomplete
    requested set; this wrapper then retries only the four required headline
    series. A true source outage still fails and leaves the prior output intact.
    """

    arguments = (
        indicator.FIRST_SOURCE_YEAR,
        date.today().year,
        indicator.REQUIRED_PERIOD,
    )
    try:
        return bls.retrieve(
            indicator.SERIES_IDS,
            *arguments,
            warning_handler=lambda warning: print(warning, file=sys.stderr),
        )
    except RetrievalError as all_series_error:
        print(
            "The complete BLS series set was unavailable; retrying the four "
            f"required headline series only. Details: {all_series_error}",
            file=sys.stderr,
        )
        return bls.retrieve(
            indicator.HEADLINE_SERIES_IDS,
            *arguments,
            warning_handler=lambda warning: print(warning, file=sys.stderr),
        )


def write_output(observations) -> None:
    """Write the standardized output atomically after all validation succeeds."""

    write_standardized_csv(OUTPUT_PATH, observations)


def print_report(
    source: bls.BlsResult,
    standardized,
    validation: dict[str, object],
) -> None:
    latest_year, headline = indicator.latest_headline_values(source.observations)
    all_years = sorted(
        {year for values in source.observations.values() for year in values}
    )
    print(f"Wrote {OUTPUT_PATH}")
    print("Retrieval succeeded: yes")
    print(f"Source method: {source.retrieval_method}")
    print(f"Years retrieved: {all_years[0]}-{all_years[-1]}")
    print(f"Latest year: {latest_year}")
    print("Latest headline rates:")
    for label, value in headline.items():
        print(f"  {label}: {indicator.decimal_text(value)} percent")
    print(f"Standardized rows: {len(standardized)}")
    print("Archive validation:")
    print(f"  overlapping rows: {validation['overlapping_rows']}")
    print(f"  exact matches: {validation['exact_matches']}")
    print(
        "  maximum absolute difference: "
        f"{indicator.decimal_text(validation['maximum_absolute_difference'])}"
    )
    mismatches = validation["mismatching_rows"]
    print(
        "  mismatching rows: "
        + (", ".join(map(str, mismatches)) if mismatches else "none")
    )
    print(
        "  non-comparable archived rows: "
        f"{validation['non_comparable_archived_rows']}"
    )
    missing_optional = indicator.missing_optional_series(source.observations)
    if missing_optional:
        print(
            "Warning: optional age series unavailable: "
            + ", ".join(missing_optional),
            file=sys.stderr,
        )
    print(f"Warning: {indicator.AGE_COVERAGE_WARNING}", file=sys.stderr)


def main() -> None:
    try:
        source = retrieve_bls_data()
        archived = indicator.read_archived_values(ARCHIVE_PATH)
        validation = indicator.validate_against_archive(
            source.observations, archived
        )
        standardized = indicator.build_standardized_observations(
            source.observations,
            archived,
            source.source_organization,
            source.source_url,
            source.retrieval_method,
            source.retrieval_date,
        )
        write_output(standardized)
        print_report(source, standardized, validation)
    except (RetrievalError, RuntimeError, ValueError) as error:
        print(
            f"Pipeline failed; existing output was not changed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
