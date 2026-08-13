#!/usr/bin/env python3
"""Fetch the shared biodiversity-ODA series for SDG 15.a.1 and 15.b.1.

The two indicators are official repeats, so this script makes one OECD SDMX
request, performs one principal-plus-significant transformation, and writes
two separately labelled standardized files.  OECD is the canonical source.
An optional UN API check and the comparison with the legacy U.S. archive are
diagnostics only; neither changes the OECD values.

Only Python's standard library is required.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sdg_pipeline.errors import RetrievalError, SourceValidationError
from sdg_pipeline.http import PROJECT_USER_AGENT, request_bytes
from sdg_pipeline.indicators import biodiversity_oda as indicator
from sdg_pipeline.output import current_retrieval_date, write_csv_outputs_atomically
from sdg_pipeline.sources import oecd
from sdg_pipeline.standardized import STANDARDIZED_COLUMNS, observation_to_row


ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
OUTPUT_PATHS = {
    "15.a.1": (
        PROJECT_ROOT / "data_processed" / "standardized" / "sdg_15_a_1.csv"
    ),
    "15.b.1": (
        PROJECT_ROOT / "data_processed" / "standardized" / "sdg_15_b_1.csv"
    ),
}
AUDIT_OUTPUT_PATH = (
    PROJECT_ROOT
    / "data_processed"
    / "audit"
    / "sdg_15_a_1_15_b_1_inputs.csv"
)

FIRST_YEAR = 2006
LAST_YEAR = 2024
REQUIRED_YEARS = tuple(range(FIRST_YEAR, LAST_YEAR + 1))

OECD_AGENCY = "OECD.DCD.FSD"
OECD_DATAFLOW = "DSD_RIOMRKR@DF_RIOMARKERS"
OECD_DATAFLOW_VERSION = "1.6"
OECD_KEY = "USA.DPGC.1000.100.2.10.1+2.C.Q._T..USD"
SOURCE_DATASET = (
    "OECD Rio Markers / Creditor Reporting System biodiversity-related "
    "official development assistance"
)

# These columns together identify the observations returned by the filtered
# Rio Markers query.  The connector preserves them so the indicator module can
# use SCORE without needing to know how the network response was encoded.
OECD_DIMENSION_COLUMNS = (
    "DONOR",
    "RECIPIENT",
    "SECTOR",
    "MEASURE",
    "ALLOCABLE",
    "MARKER",
    "SCORE",
    "FLOW_TYPE",
    "PRICE_BASE",
    "MD_DIM",
    "MD_ID",
    "UNIT_MEASURE",
)
OECD_EXPECTED_DIMENSIONS = {
    "DONOR": frozenset({"USA"}),
    "RECIPIENT": frozenset({"DPGC"}),
    "SECTOR": frozenset({"1000"}),
    "MEASURE": frozenset({"100"}),
    "ALLOCABLE": frozenset({"2"}),
    "MARKER": frozenset({"10"}),
    "SCORE": frozenset({"1", "2"}),
    "FLOW_TYPE": frozenset({"C"}),
    "PRICE_BASE": frozenset({"Q"}),
    "MD_DIM": frozenset({"_T"}),
    # OECD represents the total drilldown member as code 0 in its CSV output,
    # even though the corresponding SDMX key segment is empty.
    "MD_ID": frozenset({"0"}),
    "UNIT_MEASURE": frozenset({"USD"}),
    "BASE_PER": frozenset({"2024"}),
    # 10^6: observations are already expressed in millions of USD.
    "UNIT_MULT": frozenset({"6"}),
}

AUDIT_COLUMNS = [
    "year",
    "principal_amount",
    "significant_amount",
    "combined_amount",
    "price_basis",
    "base_period",
    "flow_type",
    "donor",
    "recipient_grouping",
    "marker",
    "source_url",
    "retrieval_date",
]

UN_SERIES_CODE = "DC_ODA_BDVDL"
UN_AREA_CODE = "840"
UN_API_ENDPOINT = "https://unstats.un.org/SDGAPI/v1/sdg/Series/Data"


@dataclass(frozen=True)
class UnComparison:
    """A non-canonical comparison with the official UN publication."""

    year: int
    oecd_value: Decimal
    un_value: Decimal
    difference: Decimal


def build_oecd_query() -> oecd.SdmxQuery:
    """Describe the exact current-methodology OECD data slice."""

    return oecd.SdmxQuery(
        agency_id=OECD_AGENCY,
        dataflow_id=OECD_DATAFLOW,
        version=OECD_DATAFLOW_VERSION,
        key=OECD_KEY,
        start_year=FIRST_YEAR,
        end_year=LAST_YEAR,
        source_dataset=SOURCE_DATASET,
        dimension_columns=OECD_DIMENSION_COLUMNS,
        expected_dimensions=OECD_EXPECTED_DIMENSIONS,
        required_years=REQUIRED_YEARS,
    )


def _un_field(row: Mapping[str, object], *names: str) -> object:
    """Read one UN field while tolerating documented casing variations."""

    for name in names:
        if name in row:
            return row[name]
    return None


def _parse_un_year(value: object) -> int:
    """Parse the UN API's integer or integer-like annual time value."""

    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise SourceValidationError(f"Invalid UN annual time period: {value!r}") from error
    if number != number.to_integral_value():
        raise SourceValidationError(f"UN time period is not annual: {value!r}")
    year = int(number)
    if year < 1900 or year > 2200:
        raise SourceValidationError(f"Invalid UN annual time period: {value!r}")
    return year


def parse_un_response(body: bytes) -> tuple[dict[int, Decimal], tuple[str, ...]]:
    """Parse optional UN observations, deduplicating only identical values."""

    if body.lstrip().startswith(b"<"):
        raise SourceValidationError("UN SDG API returned HTML or XML instead of JSON")
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceValidationError("UN SDG API returned invalid JSON") from error
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise SourceValidationError("UN SDG API response does not contain a data list")

    values: dict[int, Decimal] = {}
    duplicate_years: set[int] = set()
    for item in payload["data"]:
        if not isinstance(item, Mapping):
            raise SourceValidationError("UN SDG API data contains a malformed row")
        series = str(_un_field(item, "series", "seriesCode") or "").strip()
        area = str(_un_field(item, "refArea", "geoAreaCode", "areaCode") or "").strip()
        if series and series != UN_SERIES_CODE:
            raise SourceValidationError(f"UN SDG API returned unexpected series {series!r}")
        if area and area != UN_AREA_CODE:
            raise SourceValidationError(f"UN SDG API returned unexpected area {area!r}")

        year = _parse_un_year(
            _un_field(item, "timePeriodStart", "timePeriod", "year")
        )
        raw_value = _un_field(item, "value", "obsValue")
        try:
            value = Decimal(str(raw_value))
        except InvalidOperation as error:
            raise SourceValidationError(
                f"Invalid UN numeric observation for {year}: {raw_value!r}"
            ) from error
        if not value.is_finite():
            raise SourceValidationError(
                f"UN observation for {year} is not a finite number"
            )

        existing = values.get(year)
        if existing is not None:
            if existing != value:
                raise SourceValidationError(
                    "UN SDG API returned conflicting duplicate observations for "
                    f"{year}: {existing} and {value}"
                )
            duplicate_years.add(year)
            continue
        values[year] = value

    if not values:
        raise SourceValidationError("UN SDG API returned no usable observations")
    warnings = tuple(
        f"Ignored identical duplicate UN observation for {year}"
        for year in sorted(duplicate_years)
    )
    return values, warnings


def fetch_optional_un_values() -> tuple[dict[int, Decimal], tuple[str, ...], str]:
    """Retrieve the non-blocking official UN cross-check series."""

    parameters = urllib.parse.urlencode(
        {
            "seriesCode": UN_SERIES_CODE,
            "areaCode": UN_AREA_CODE,
            "page": 1,
            "pageSize": 100,
        }
    )
    source_url = f"{UN_API_ENDPOINT}?{parameters}"
    request = urllib.request.Request(
        source_url,
        headers={"Accept": "application/json", "User-Agent": PROJECT_USER_AGENT},
    )
    body, content_type = request_bytes(request, display_url=source_url)
    if content_type not in {"application/json", "text/json", "text/plain"}:
        raise SourceValidationError(
            f"UN SDG API returned unexpected content type {content_type!r}"
        )
    values, warnings = parse_un_response(body)
    return values, warnings, source_url


def compare_with_un(
    calculated: Sequence[indicator.BiodiversityOdaYear],
    un_values: Mapping[int, Decimal],
) -> list[UnComparison]:
    """Compare common years without making UN availability a requirement."""

    oecd_values = {item.year: item.combined for item in calculated}
    return [
        UnComparison(
            year=year,
            oecd_value=oecd_values[year],
            un_value=un_values[year],
            difference=oecd_values[year] - un_values[year],
        )
        for year in sorted(set(oecd_values) & set(un_values))
    ]


def build_audit_rows(
    calculated: Sequence[indicator.BiodiversityOdaYear],
    source: oecd.OecdResult,
) -> list[dict[str, object]]:
    """Build a transparent principal/significant input record for each year."""

    return [
        {
            "year": item.year,
            "principal_amount": indicator.decimal_text(item.principal),
            "significant_amount": indicator.decimal_text(item.significant),
            "combined_amount": indicator.decimal_text(item.combined),
            "price_basis": "constant prices",
            "base_period": "2024",
            "flow_type": "commitments",
            "donor": "United States",
            "recipient_grouping": "developing countries",
            "marker": "biodiversity (principal + significant)",
            "source_url": source.source_url,
            "retrieval_date": source.retrieval_date,
        }
        for item in calculated
    ]


def write_outputs(
    standardized: Mapping[str, Sequence[object]],
    audit_rows: Sequence[Mapping[str, object]],
) -> None:
    """Prepare every output before replacing any prior successful file."""

    outputs = [
        (
            OUTPUT_PATHS[indicator_id],
            STANDARDIZED_COLUMNS,
            [observation_to_row(item) for item in standardized[indicator_id]],
        )
        for indicator_id in indicator.INDICATOR_IDS
    ]
    outputs.append((AUDIT_OUTPUT_PATH, AUDIT_COLUMNS, audit_rows))
    write_csv_outputs_atomically(outputs)


def print_report(
    source: oecd.OecdResult,
    calculated: Sequence[indicator.BiodiversityOdaYear],
    archive_comparison: Sequence[indicator.ArchiveComparison],
    un_comparison: Sequence[UnComparison] | None,
    un_warning: str | None,
    standardized: Mapping[str, Sequence[object]],
) -> None:
    """Explain live results and the deliberate archive methodology break."""

    years = [item.year for item in calculated]
    latest = calculated[-1]
    print("Retrieval succeeded: yes")
    print(f"Source method: {source.retrieval_method}")
    print("Years retrieved: " + ", ".join(map(str, years)))
    print(f"Latest year: {latest.year}")
    print(f"Latest principal amount: {indicator.decimal_text(latest.principal)}")
    print(f"Latest significant amount: {indicator.decimal_text(latest.significant)}")
    print(f"Latest combined value: {indicator.decimal_text(latest.combined)}")
    for indicator_id in indicator.INDICATOR_IDS:
        print(
            f"Standardized rows for {indicator_id}: "
            f"{len(standardized[indicator_id])}"
        )
        print(f"Wrote {OUTPUT_PATHS[indicator_id]}")
    print(f"Wrote {AUDIT_OUTPUT_PATH}")

    if un_warning is not None:
        print(f"Optional UN validation unavailable: {un_warning}")
    else:
        assert un_comparison is not None
        exact = sum(item.difference == 0 for item in un_comparison)
        mismatches = [item for item in un_comparison if item.difference != 0]
        print("Optional OECD-versus-UN validation:")
        print(f"  overlapping years: {len(un_comparison)}")
        print(f"  exact matches: {exact}")
        print(
            "  differing years: "
            + (", ".join(str(item.year) for item in mismatches) or "none")
        )
        for item in mismatches:
            print(
                f"    {item.year}: OECD={indicator.decimal_text(item.oecd_value)}, "
                f"UN={indicator.decimal_text(item.un_value)}, "
                f"difference={indicator.decimal_text(item.difference)}"
            )

    print("Archive diagnostic (methodology break; equality is not expected):")
    print(f"  overlapping years: {len(archive_comparison)}")
    print(
        "  archived methodology: "
        + indicator.ARCHIVED_METHODOLOGY
    )
    print("  current methodology: " + indicator.CURRENT_METHODOLOGY)
    print("  year | archived | current OECD | difference")
    for item in archive_comparison:
        print(
            f"  {item.year} | {indicator.decimal_text(item.archived_value)} | "
            f"{indicator.decimal_text(item.current_value)} | "
            f"{indicator.decimal_text(item.difference)}"
        )


def main() -> None:
    try:
        retrieval_date = current_retrieval_date()
        source = oecd.fetch_sdmx_csv(
            build_oecd_query(), retrieval_date=retrieval_date
        )
        result = indicator.calculate(source.observations, REQUIRED_YEARS)
        for warning in (*source.warnings, *result.warnings):
            print(f"Warning: {warning}", file=sys.stderr)

        standardized = {
            indicator_id: indicator.build_standardized_observations(
                indicator_id, result.years, source
            )
            for indicator_id in indicator.INDICATOR_IDS
        }
        archived = indicator.read_archived_values(ARCHIVE_PATH)
        archive_comparison = indicator.compare_with_archive(result.years, archived)

        un_comparison = None
        un_warning = None
        try:
            un_values, un_warnings, _un_url = fetch_optional_un_values()
            for warning in un_warnings:
                print(f"Warning: {warning}", file=sys.stderr)
            un_comparison = compare_with_un(result.years, un_values)
        except RetrievalError as error:
            # OECD remains canonical. A temporary UN failure must never prevent
            # a successfully retrieved OECD result from being published.
            un_warning = str(error)

        audit_rows = build_audit_rows(result.years, source)
        write_outputs(standardized, audit_rows)
        print_report(
            source,
            result.years,
            archive_comparison,
            un_comparison,
            un_warning,
            standardized,
        )
    except (RetrievalError, RuntimeError, OSError, ValueError) as error:
        print(
            f"Pipeline failed; existing outputs were not changed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
