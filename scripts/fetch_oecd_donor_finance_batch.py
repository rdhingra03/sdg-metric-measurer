#!/usr/bin/env python3
"""Fetch the coordinated OECD donor-finance batch for three U.S. SDGs.

The batch publishes current standardized outputs for SDG 8.a.1, 10.b.1, and
17.2.1.  It deliberately does not create U.S.-donor pipelines for the four
recipient-only indicators 2.a.2, 3.b.2, 4.b.1, and 9.a.1.

OECD is the canonical source.  UN comparisons and legacy archive comparisons
are diagnostics only and cannot change the calculated OECD values.
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
from sdg_pipeline.indicators import oecd_donor_finance as indicator
from sdg_pipeline.output import current_retrieval_date, write_csv_outputs_atomically
from sdg_pipeline.sources import oecd
from sdg_pipeline.standardized import STANDARDIZED_COLUMNS, observation_to_row


ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
STANDARDIZED_PATHS = {
    "8.a.1": PROJECT_ROOT / "data_processed/standardized/sdg_8_a_1.csv",
    "10.b.1": PROJECT_ROOT / "data_processed/standardized/sdg_10_b_1.csv",
    "17.2.1": PROJECT_ROOT / "data_processed/standardized/sdg_17_2_1.csv",
}
AUDIT_PATHS = {
    "8.a.1": PROJECT_ROOT / "data_processed/audit/sdg_8_a_1_inputs.csv",
    "10.b.1": PROJECT_ROOT / "data_processed/audit/sdg_10_b_1_inputs.csv",
    "17.2.1": PROJECT_ROOT / "data_processed/audit/sdg_17_2_1_inputs.csv",
}

OECD_AGENCY = "OECD.DCD.FSD"
OECD_CRS_FLOW = "DSD_CRS@DF_CRS"
OECD_CRS_VERSION = "1.6"
OECD_DAC1_FLOW = "DSD_DAC1@DF_DAC1"
OECD_DAC1_VERSION = "1.7"
OECD_DAC2A_FLOW = "DSD_DAC2@DF_DAC2A"
OECD_DAC2A_VERSION = "1.6"
OECD_PUBLIC_DATA_ENDPOINT = "https://sdmx.oecd.org/public/rest/data"

FIRST_YEAR = 2000
LAST_YEAR = 2024
COMMON_YEARS = tuple(range(FIRST_YEAR, LAST_YEAR + 1))
AID_FOR_TRADE_YEARS_BY_FLOW = {
    "C": tuple(range(2006, LAST_YEAR + 1)),
    "D": tuple(range(2002, LAST_YEAR + 1)),
}
GRANT_EQUIVALENT_YEARS = tuple(range(2018, LAST_YEAR + 1))

AID_FOR_TRADE_DATASET = (
    "OECD Creditor Reporting System (CRS) Aid for Trade flows"
)
DAC1_DATASET = "OECD DAC1: Flows by provider (ODA+OOF+Private)"
DAC2A_DATASET = "OECD DAC2A: Aid (ODA) disbursements to countries and regions"

AID_FOR_TRADE_DIMENSIONS = (
    "DONOR",
    "RECIPIENT",
    "SECTOR",
    "MEASURE",
    "CHANNEL",
    "MODALITY",
    "FLOW_TYPE",
    "PRICE_BASE",
    "MD_DIM",
    "MD_ID",
    "UNIT_MEASURE",
)
DAC1_DIMENSIONS = (
    "DONOR",
    "SECTOR",
    "MEASURE",
    "TYING_STATUS",
    "FLOW_TYPE",
    "UNIT_MEASURE",
    "PRICE_BASE",
)
DAC2A_DIMENSIONS = (
    "DONOR",
    "RECIPIENT",
    "MEASURE",
    "FLOW_TYPE",
    "UNIT_MEASURE",
    "PRICE_BASE",
)

AID_FOR_TRADE_AUDIT_COLUMNS = [
    "year",
    "flow_code",
    "flow",
    "sector",
    "sector_value",
    "source_observation_present",
    "annual_total",
    "measure",
    "price_basis",
    "base_period",
    "unit",
    "donor",
    "recipient",
    "source_url",
    "retrieval_date",
]
RESOURCE_FLOWS_AUDIT_COLUMNS = [
    "year",
    "source_measure",
    "measure_label",
    "raw_value",
    "flow_type",
    "price_basis",
    "unit",
    "donor",
    "source_url",
    "retrieval_date",
]
ODA_GNI_AUDIT_COLUMNS = [
    "year",
    "net_oda",
    "gni",
    "bilateral_net_ldc_oda",
    "imputed_multilateral_ldc_oda",
    "total_oda_gni_percent",
    "ldc_oda_gni_percent",
    "grant_equivalent_oda",
    "grant_equivalent_percent",
    "grant_equivalent_minus_net_percent",
    "dac1_source_url",
    "dac2a_source_url",
    "grant_equivalent_source_url",
    "retrieval_date",
]

UN_AREA_CODE = "840"
UN_API_ENDPOINT = "https://unstats.un.org/SDGAPI/v1/sdg/Series/Data"
UN_SERIES = {
    "8.a.1 commitments": "DC_TOF_TRDCMDL",
    "8.a.1 disbursements": "DC_TOF_TRDDBMDL",
    "10.b.1": "DC_TRF_TOTDL",
    "17.2.1 total net": "DC_ODA_TOTG",
    "17.2.1 LDC net": "DC_ODA_LDCG",
    "17.2.1 grant equivalent": "DC_ODA_TOTGGE",
}


@dataclass(frozen=True)
class UnComparison:
    """One non-canonical OECD-versus-UN comparison."""

    year: int
    oecd_value: Decimal
    un_value: Decimal
    difference: Decimal


def build_aid_for_trade_query() -> oecd.SdmxQuery:
    """Describe the exact current 8.a.1 CRS slice."""

    sectors = "+".join(indicator.AID_FOR_TRADE_SECTORS)
    return oecd.SdmxQuery(
        agency_id=OECD_AGENCY,
        dataflow_id=OECD_CRS_FLOW,
        version=OECD_CRS_VERSION,
        key=f"USA.DPGC.{sectors}.100._T._T.C+D.Q._T..USD",
        start_year=2002,
        end_year=LAST_YEAR,
        source_dataset=AID_FOR_TRADE_DATASET,
        dimension_columns=AID_FOR_TRADE_DIMENSIONS,
        expected_dimensions={
            "DONOR": frozenset({"USA"}),
            "RECIPIENT": frozenset({"DPGC"}),
            "SECTOR": frozenset(indicator.AID_FOR_TRADE_SECTORS),
            "MEASURE": frozenset({"100"}),
            "CHANNEL": frozenset({"_T"}),
            "MODALITY": frozenset({"_T"}),
            "FLOW_TYPE": frozenset({"C", "D"}),
            "PRICE_BASE": frozenset({"Q"}),
            "MD_DIM": frozenset({"_T"}),
            "MD_ID": frozenset({"0"}),
            "UNIT_MEASURE": frozenset({"USD"}),
            "BASE_PER": frozenset({"2024"}),
            "UNIT_MULT": frozenset({"6"}),
        },
        required_years=tuple(range(2002, LAST_YEAR + 1)),
    )


def build_dac1_query() -> oecd.SdmxQuery:
    """Retrieve shared DAC1 inputs for 10.b.1 and 17.2.1 once."""

    return oecd.SdmxQuery(
        agency_id=OECD_AGENCY,
        dataflow_id=OECD_DAC1_FLOW,
        version=OECD_DAC1_VERSION,
        key="USA._Z.1+5+1010..1140.USD.V",
        start_year=FIRST_YEAR,
        end_year=LAST_YEAR,
        source_dataset=DAC1_DATASET,
        dimension_columns=DAC1_DIMENSIONS,
        expected_dimensions={
            "DONOR": frozenset({"USA"}),
            "SECTOR": frozenset({"_Z"}),
            "MEASURE": frozenset({"1", "5", "1010"}),
            "TYING_STATUS": frozenset({"_Z"}),
            "FLOW_TYPE": frozenset({"1140"}),
            "UNIT_MEASURE": frozenset({"USD"}),
            "PRICE_BASE": frozenset({"V"}),
            "UNIT_MULT": frozenset({"6"}),
        },
        required_years=COMMON_YEARS,
        endpoint=OECD_PUBLIC_DATA_ENDPOINT,
    )


def build_dac2a_query() -> oecd.SdmxQuery:
    """Retrieve both LDC numerator components for 17.2.1 once."""

    return oecd.SdmxQuery(
        agency_id=OECD_AGENCY,
        dataflow_id=OECD_DAC2A_FLOW,
        version=OECD_DAC2A_VERSION,
        key="USA.LDC.106+206.USD.V",
        start_year=FIRST_YEAR,
        end_year=LAST_YEAR,
        source_dataset=DAC2A_DATASET,
        dimension_columns=DAC2A_DIMENSIONS,
        expected_dimensions={
            "DONOR": frozenset({"USA"}),
            "RECIPIENT": frozenset({"LDC"}),
            "MEASURE": frozenset({"106", "206"}),
            "FLOW_TYPE": frozenset({"D"}),
            "UNIT_MEASURE": frozenset({"USD"}),
            "PRICE_BASE": frozenset({"V"}),
            "UNIT_MULT": frozenset({"6"}),
        },
        required_years=COMMON_YEARS,
        endpoint=OECD_PUBLIC_DATA_ENDPOINT,
    )


def build_grant_equivalent_query() -> oecd.SdmxQuery:
    """Describe the optional post-2018 DAC1 audit series."""

    return oecd.SdmxQuery(
        agency_id=OECD_AGENCY,
        dataflow_id=OECD_DAC1_FLOW,
        version=OECD_DAC1_VERSION,
        key="USA._Z.11010..1160.USD.V",
        start_year=2018,
        end_year=LAST_YEAR,
        source_dataset=DAC1_DATASET,
        dimension_columns=DAC1_DIMENSIONS,
        expected_dimensions={
            "DONOR": frozenset({"USA"}),
            "SECTOR": frozenset({"_Z"}),
            "MEASURE": frozenset({"11010"}),
            "TYING_STATUS": frozenset({"_Z"}),
            "FLOW_TYPE": frozenset({"1160"}),
            "UNIT_MEASURE": frozenset({"USD"}),
            "PRICE_BASE": frozenset({"V"}),
            "UNIT_MULT": frozenset({"6"}),
        },
        required_years=GRANT_EQUIVALENT_YEARS,
        endpoint=OECD_PUBLIC_DATA_ENDPOINT,
    )


def _un_field(row: Mapping[str, object], *names: str) -> object:
    """Read one UN field while tolerating documented naming variations."""

    for name in names:
        if name in row:
            return row[name]
    return None


def parse_un_response(
    body: bytes, expected_series: str, expected_area: str = UN_AREA_CODE
) -> tuple[dict[int, Decimal], tuple[str, ...]]:
    """Parse one optional UN series and safely handle duplicate observations."""

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
        area = str(
            _un_field(item, "refArea", "geoAreaCode", "areaCode") or ""
        ).strip()
        if series and series != expected_series:
            raise SourceValidationError(f"UN SDG API returned unexpected series {series!r}")
        if area and area != expected_area:
            raise SourceValidationError(f"UN SDG API returned unexpected area {area!r}")

        raw_year = _un_field(item, "timePeriodStart", "timePeriod", "year")
        try:
            numeric_year = Decimal(str(raw_year))
        except InvalidOperation as error:
            raise SourceValidationError(f"Invalid UN annual time period: {raw_year!r}") from error
        if numeric_year != numeric_year.to_integral_value():
            raise SourceValidationError(f"UN time period is not annual: {raw_year!r}")
        year = int(numeric_year)

        raw_value = _un_field(item, "value", "obsValue")
        try:
            value = Decimal(str(raw_value))
        except InvalidOperation as error:
            raise SourceValidationError(
                f"Invalid UN numeric observation for {year}: {raw_value!r}"
            ) from error
        if not value.is_finite():
            raise SourceValidationError(f"UN observation for {year} is not finite")

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
    return values, tuple(
        f"Ignored identical duplicate UN observation for {year}"
        for year in sorted(duplicate_years)
    )


def fetch_optional_un_series(series_code: str) -> tuple[dict[int, Decimal], tuple[str, ...]]:
    """Retrieve one non-blocking official UN comparison series."""

    parameters = urllib.parse.urlencode(
        {
            "seriesCode": series_code,
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
    return parse_un_response(body, series_code)


def compare_values(
    calculated: Mapping[int, Decimal], published: Mapping[int, Decimal]
) -> list[UnComparison]:
    """Compare common years without making UN availability mandatory."""

    return [
        UnComparison(year, calculated[year], published[year], calculated[year] - published[year])
        for year in sorted(set(calculated) & set(published))
    ]


def build_aid_for_trade_audit_rows(
    result: indicator.AidForTradeResult, source: oecd.OecdResult
) -> list[dict[str, object]]:
    """Retain every sector cell and its annual total."""

    totals = {(item.year, item.flow): item.value for item in result.totals}
    return [
        {
            "year": item.year,
            "flow_code": item.flow,
            "flow": indicator.AID_FOR_TRADE_FLOW_LABELS[item.flow],
            "sector": item.sector,
            "sector_value": indicator.decimal_text(item.value),
            "source_observation_present": str(item.source_observation_present).lower(),
            "annual_total": indicator.decimal_text(totals[(item.year, item.flow)]),
            "measure": "100 - Official Development Assistance",
            "price_basis": "constant prices",
            "base_period": "2024",
            "unit": "USD millions",
            "donor": "United States",
            "recipient": "Developing countries",
            "source_url": source.source_url,
            "retrieval_date": source.retrieval_date,
        }
        for item in result.components
    ]


def build_resource_flows_audit_rows(
    calculated: Sequence[indicator.ResourceFlowYear], source: oecd.OecdResult
) -> list[dict[str, object]]:
    """Retain the direct DAC1 measure used by 10.b.1."""

    return [
        {
            "year": item.year,
            "source_measure": "5",
            "measure_label": "Official and private flows",
            "raw_value": indicator.decimal_text(item.value),
            "flow_type": "1140 - Net disbursements",
            "price_basis": "current prices",
            "unit": "USD millions",
            "donor": "United States",
            "source_url": source.source_url,
            "retrieval_date": source.retrieval_date,
        }
        for item in calculated
    ]


def build_oda_gni_audit_rows(
    calculated: Sequence[indicator.OdaGniYear],
    dac1_source: oecd.OecdResult,
    dac2a_source: oecd.OecdResult,
    grant_equivalent_source: oecd.OecdResult | None,
) -> list[dict[str, object]]:
    """Retain every numerator, denominator, percentage, and audit comparison."""

    ge_url = grant_equivalent_source.source_url if grant_equivalent_source else ""
    rows = []
    for item in calculated:
        ge_difference = (
            item.grant_equivalent_percent - item.total_percent
            if item.grant_equivalent_percent is not None
            else None
        )
        rows.append(
            {
                "year": item.year,
                "net_oda": indicator.decimal_text(item.net_oda),
                "gni": indicator.decimal_text(item.gni),
                "bilateral_net_ldc_oda": indicator.decimal_text(
                    item.ldc_bilateral_net_oda
                ),
                "imputed_multilateral_ldc_oda": indicator.decimal_text(
                    item.ldc_imputed_multilateral_oda
                ),
                "total_oda_gni_percent": indicator.decimal_text(item.total_percent),
                "ldc_oda_gni_percent": indicator.decimal_text(item.ldc_percent),
                "grant_equivalent_oda": (
                    indicator.decimal_text(item.grant_equivalent_oda)
                    if item.grant_equivalent_oda is not None
                    else ""
                ),
                "grant_equivalent_percent": (
                    indicator.decimal_text(item.grant_equivalent_percent)
                    if item.grant_equivalent_percent is not None
                    else ""
                ),
                "grant_equivalent_minus_net_percent": (
                    indicator.decimal_text(ge_difference)
                    if ge_difference is not None
                    else ""
                ),
                "dac1_source_url": dac1_source.source_url,
                "dac2a_source_url": dac2a_source.source_url,
                "grant_equivalent_source_url": ge_url,
                "retrieval_date": dac1_source.retrieval_date,
            }
        )
    return rows


def write_outputs(
    standardized: Mapping[str, Sequence[object]],
    aid_for_trade_audit: Sequence[Mapping[str, object]],
    resource_flows_audit: Sequence[Mapping[str, object]],
    oda_gni_audit: Sequence[Mapping[str, object]],
) -> None:
    """Prepare all six outputs before replacing any successful prior output."""

    outputs = [
        (
            STANDARDIZED_PATHS[indicator_id],
            STANDARDIZED_COLUMNS,
            [observation_to_row(item) for item in standardized[indicator_id]],
        )
        for indicator_id in ("8.a.1", "10.b.1", "17.2.1")
    ]
    outputs.extend(
        [
            (
                AUDIT_PATHS["8.a.1"],
                AID_FOR_TRADE_AUDIT_COLUMNS,
                aid_for_trade_audit,
            ),
            (
                AUDIT_PATHS["10.b.1"],
                RESOURCE_FLOWS_AUDIT_COLUMNS,
                resource_flows_audit,
            ),
            (
                AUDIT_PATHS["17.2.1"],
                ODA_GNI_AUDIT_COLUMNS,
                oda_gni_audit,
            ),
        ]
    )
    write_csv_outputs_atomically(outputs)


def _comparison_summary(items: Sequence[UnComparison]) -> str:
    """Return a concise overlap/exact/max-difference summary."""

    if not items:
        return "no overlapping years"
    matches_at_published_precision = sum(
        item.oecd_value.quantize(
            Decimal(1).scaleb(item.un_value.as_tuple().exponent)
        )
        == item.un_value
        for item in items
    )
    maximum = max(abs(item.difference) for item in items)
    mismatches = [str(item.year) for item in items if item.difference != 0]
    return (
        f"{len(items)} overlaps; {matches_at_published_precision} match at UN "
        "published precision; max abs difference "
        f"{indicator.decimal_text(maximum)}; differing years "
        + (", ".join(mismatches) or "none")
    )


def print_report(
    aid_for_trade: indicator.AidForTradeResult,
    resource_flows: Sequence[indicator.ResourceFlowYear],
    oda_gni: Sequence[indicator.OdaGniYear],
    archive_comparison: Sequence[indicator.ArchiveComparison],
    placeholder_10: indicator.PlaceholderArchive,
    placeholder_17: indicator.PlaceholderArchive,
    un_comparisons: Mapping[str, Sequence[UnComparison]],
    un_warnings: Sequence[str],
    grant_equivalent_warning: str | None,
) -> None:
    """Print the requested live, UN, and archive validation report."""

    aid_by_key = {(item.year, item.flow): item for item in aid_for_trade.totals}
    latest_aid_year = max(item.year for item in aid_for_trade.totals)
    latest_resource = resource_flows[-1]
    latest_oda = oda_gni[-1]

    print("Retrieval succeeded: yes")
    print("Source method: OECD SDMX API")
    print("8.a.1 years retrieved:")
    for flow in ("C", "D"):
        years = [item.year for item in aid_for_trade.totals if item.flow == flow]
        print(
            f"  {indicator.AID_FOR_TRADE_FLOW_LABELS[flow]}: "
            f"{min(years)}-{max(years)} ({len(years)} years)"
        )
    print(
        "8.a.1 latest commitment: "
        + indicator.decimal_text(aid_by_key[(latest_aid_year, "C")].value)
    )
    print(
        "8.a.1 latest disbursement: "
        + indicator.decimal_text(aid_by_key[(latest_aid_year, "D")].value)
    )
    print(
        f"10.b.1 years retrieved: {resource_flows[0].year}-{latest_resource.year} "
        f"({len(resource_flows)} years)"
    )
    print(f"10.b.1 latest value: {indicator.decimal_text(latest_resource.value)}")
    print(
        f"17.2.1 years retrieved: {oda_gni[0].year}-{latest_oda.year} "
        f"({len(oda_gni)} years, two components each)"
    )
    print(
        "17.2.1 latest total net ODA/GNI: "
        + indicator.decimal_text(latest_oda.total_percent)
    )
    print(
        "17.2.1 latest LDC net ODA/GNI: "
        + indicator.decimal_text(latest_oda.ldc_percent)
    )
    if latest_oda.grant_equivalent_percent is not None:
        print(
            "17.2.1 latest grant-equivalent audit percentage: "
            + indicator.decimal_text(latest_oda.grant_equivalent_percent)
        )
    if grant_equivalent_warning:
        print("Grant-equivalent audit unavailable: " + grant_equivalent_warning)

    print("Optional UN cross-checks:")
    for label in UN_SERIES:
        if label in un_comparisons:
            print(f"  {label}: {_comparison_summary(un_comparisons[label])}")
    for warning in un_warnings:
        print(f"  unavailable: {warning}")

    differences = [abs(item.difference) for item in archive_comparison]
    print("Archive diagnostics:")
    print(
        f"  8.a.1: {len(archive_comparison)} overlaps across different price "
        "bases; equality not required; max abs difference "
        + indicator.decimal_text(max(differences))
    )
    print(
        f"  10.b.1: {placeholder_10.year} zero classified as placeholder; "
        f"calculated 2015 value {indicator.decimal_text(next(item.value for item in resource_flows if item.year == 2015))}"
    )
    official_2015 = next(item for item in oda_gni if item.year == 2015)
    print(
        f"  17.2.1: {placeholder_17.year} zero classified as placeholder; "
        "calculated 2015 total/LDC percentages "
        f"{indicator.decimal_text(official_2015.total_percent)} / "
        f"{indicator.decimal_text(official_2015.ldc_percent)}"
    )
    for path in (*STANDARDIZED_PATHS.values(), *AUDIT_PATHS.values()):
        print(f"Wrote {path}")


def main() -> None:
    """Retrieve, calculate, validate diagnostically, and publish the batch."""

    try:
        retrieval_date = current_retrieval_date()
        aid_source = oecd.fetch_sdmx_csv(
            build_aid_for_trade_query(), retrieval_date=retrieval_date
        )
        dac1_source = oecd.fetch_sdmx_csv(
            build_dac1_query(), retrieval_date=retrieval_date
        )
        dac2a_source = oecd.fetch_sdmx_csv(
            build_dac2a_query(), retrieval_date=retrieval_date
        )

        grant_equivalent_source = None
        grant_equivalent_warning = None
        try:
            grant_equivalent_source = oecd.fetch_sdmx_csv(
                build_grant_equivalent_query(), retrieval_date=retrieval_date
            )
        except RetrievalError as error:
            grant_equivalent_warning = str(error)

        aid_result = indicator.calculate_aid_for_trade(
            aid_source.observations, AID_FOR_TRADE_YEARS_BY_FLOW
        )
        for warning in (*aid_source.warnings, *aid_result.warnings):
            print(f"Warning: {warning}", file=sys.stderr)
        resource_result = indicator.calculate_resource_flows(
            dac1_source.observations, COMMON_YEARS
        )
        oda_result = indicator.calculate_oda_gni(
            dac1_source.observations,
            dac2a_source.observations,
            COMMON_YEARS,
            grant_equivalent_source.observations if grant_equivalent_source else (),
        )

        standardized = {
            "8.a.1": indicator.build_aid_for_trade_standardized(
                aid_result.totals, aid_source
            ),
            "10.b.1": indicator.build_resource_flows_standardized(
                resource_result, dac1_source
            ),
            "17.2.1": indicator.build_oda_gni_standardized(
                oda_result, dac1_source, dac2a_source
            ),
        }

        archived_aid = indicator.read_aid_for_trade_archive(ARCHIVE_PATH)
        archive_comparison = indicator.compare_aid_for_trade_archive(
            aid_result.totals, archived_aid
        )
        placeholder_10, placeholder_17 = indicator.read_placeholder_archives(
            ARCHIVE_PATH
        )

        calculated_un_maps = {
            "8.a.1 commitments": {
                item.year: item.value for item in aid_result.totals if item.flow == "C"
            },
            "8.a.1 disbursements": {
                item.year: item.value for item in aid_result.totals if item.flow == "D"
            },
            "10.b.1": {item.year: item.value for item in resource_result},
            "17.2.1 total net": {item.year: item.total_percent for item in oda_result},
            "17.2.1 LDC net": {item.year: item.ldc_percent for item in oda_result},
            "17.2.1 grant equivalent": {
                item.year: item.grant_equivalent_percent
                for item in oda_result
                if item.grant_equivalent_percent is not None
            },
        }
        un_comparisons: dict[str, Sequence[UnComparison]] = {}
        un_warnings: list[str] = []
        for label, series_code in UN_SERIES.items():
            try:
                un_values, warnings = fetch_optional_un_series(series_code)
                for warning in warnings:
                    print(f"Warning: {warning}", file=sys.stderr)
                un_comparisons[label] = compare_values(
                    calculated_un_maps[label], un_values
                )
            except RetrievalError as error:
                un_warnings.append(f"{label}: {error}")

        aid_audit = build_aid_for_trade_audit_rows(aid_result, aid_source)
        resource_audit = build_resource_flows_audit_rows(
            resource_result, dac1_source
        )
        oda_audit = build_oda_gni_audit_rows(
            oda_result, dac1_source, dac2a_source, grant_equivalent_source
        )
        write_outputs(
            standardized, aid_audit, resource_audit, oda_audit
        )
        print_report(
            aid_result,
            resource_result,
            oda_result,
            archive_comparison,
            placeholder_10,
            placeholder_17,
            un_comparisons,
            un_warnings,
            grant_equivalent_warning,
        )
    except (RetrievalError, RuntimeError, OSError, ValueError) as error:
        print(
            f"Pipeline failed; existing outputs were not changed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
