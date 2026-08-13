"""SDG transformations for the coordinated OECD donor-finance batch.

This module contains only indicator meaning and arithmetic.  It receives
already-parsed observations from the generic OECD connector and therefore
does not know how SDMX URLs, HTTP requests, or CSV responses work.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from ..archive import ArchiveReadError, read_nested_zip_member
from ..sources.oecd import OecdResult, SdmxObservation
from ..standardized import CURRENT_METHODOLOGY_VERIFIED, StandardizedObservation


CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
ARCHIVE_8_A_1 = "sdg-master/data/indicator_8-a-1.csv"
ARCHIVE_10_B_1 = "sdg-master/data/indicator_10-b-1.csv"
ARCHIVE_17_2_1 = "sdg-master/data/indicator_17-2-1.csv"

GEOGRAPHY = "United States"

AID_FOR_TRADE_INDICATOR_ID = "8.a.1"
AID_FOR_TRADE_TITLE = "Aid for Trade commitments and disbursements"
AID_FOR_TRADE_SECTORS = (
    "210",
    "220",
    "230",
    "240",
    "250",
    "310",
    "320",
    "331",
    "332",
)
AID_FOR_TRADE_FLOW_LABELS = {
    "C": "Commitments",
    "D": "Disbursements",
}
AID_FOR_TRADE_UNIT = "million constant 2024 USD"
AID_FOR_TRADE_METHODOLOGY = "oecd_us_donor_aid_for_trade_constant_2024"
AID_FOR_TRADE_WARNING = (
    "The current OECD/UN method uses commitments and gross disbursements in "
    "constant 2024 USD. The legacy U.S. archive uses current USD, so archive "
    "differences are a price-basis diagnostic and are not validation failures."
)

RESOURCE_FLOWS_INDICATOR_ID = "10.b.1"
RESOURCE_FLOWS_TITLE = (
    "Total resource flows for development (e.g. official development "
    "assistance, foreign direct investment and other flows)"
)
RESOURCE_FLOWS_UNIT = "million current USD"
RESOURCE_FLOWS_METHODOLOGY = "oecd_us_donor_total_resource_flows_net_current"
RESOURCE_FLOWS_WARNING = (
    "This is the current OECD donor-flow implementation: official and private "
    "flows on a net-disbursement, current-price basis. The archived 2015 zero "
    "is a placeholder and is not validation data."
)

ODA_GNI_INDICATOR_ID = "17.2.1"
ODA_GNI_TITLE = (
    "Net official development assistance, total and to least developed "
    "countries, as a proportion of OECD Development Assistance Committee "
    "donors' gross national income (GNI)"
)
ODA_GNI_UNIT = "percent of GNI"
ODA_GNI_METHODOLOGY = "oecd_net_oda_percent_gni"
ODA_GNI_WARNING = (
    "The canonical calculation uses formal net ODA flows. OECD also publishes "
    "post-2018 headline ODA on a grant-equivalent basis; that series is kept "
    "only as an audit comparison and is not substituted for net ODA. The "
    "archived 2015 zero is a placeholder and is not validation data."
)

TOTAL_COMPONENT = "Total ODA"
LDC_COMPONENT = "Least developed countries"


def decimal_text(value: Decimal) -> str:
    """Return non-scientific decimal text without discarding precision."""

    return format(value, "f")


@dataclass(frozen=True)
class AidForTradeComponent:
    """One sector contribution retained for the 8.a.1 audit trail."""

    year: int
    flow: str
    sector: str
    value: Decimal
    source_observation_present: bool


@dataclass(frozen=True)
class AidForTradeTotal:
    """One annual commitment or disbursement total."""

    year: int
    flow: str
    value: Decimal


@dataclass(frozen=True)
class AidForTradeResult:
    """Calculated totals, their components, and non-fatal zero-cell warnings."""

    totals: tuple[AidForTradeTotal, ...]
    components: tuple[AidForTradeComponent, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ResourceFlowYear:
    """One annual 10.b.1 total-resource-flow value."""

    year: int
    value: Decimal


@dataclass(frozen=True)
class OdaGniYear:
    """Inputs and both calculated components of 17.2.1 for one year."""

    year: int
    net_oda: Decimal
    gni: Decimal
    ldc_bilateral_net_oda: Decimal
    ldc_imputed_multilateral_oda: Decimal
    total_percent: Decimal
    ldc_percent: Decimal
    grant_equivalent_oda: Decimal | None = None
    grant_equivalent_percent: Decimal | None = None


@dataclass(frozen=True)
class ArchiveComparison:
    """Diagnostic comparison across intentionally different price bases."""

    year: int
    category: str
    archived_value: Decimal
    current_value: Decimal
    difference: Decimal


@dataclass(frozen=True)
class PlaceholderArchive:
    """A recognized legacy placeholder that must never validate a result."""

    year: int
    value: Decimal
    is_placeholder: bool = True


def calculate_aid_for_trade(
    observations: Sequence[SdmxObservation],
    required_years_by_flow: Mapping[str, Sequence[int]],
) -> AidForTradeResult:
    """Sum the nine required Aid-for-Trade sectors separately by flow.

    OECD omits aggregate cells whose value is zero.  An omitted sector is
    therefore represented as an auditable zero, but a year/flow combination
    with no source observations at all is rejected.
    """

    expected_flows = set(required_years_by_flow)
    if not expected_flows <= set(AID_FOR_TRADE_FLOW_LABELS):
        raise ValueError("Unsupported Aid-for-Trade flow")

    by_key: dict[tuple[int, str, str], Decimal] = {}
    for observation in observations:
        flow = observation.dimension("FLOW_TYPE")
        sector = observation.dimension("SECTOR")
        if flow not in expected_flows:
            raise RuntimeError(f"Unexpected Aid-for-Trade flow {flow!r}")
        if sector not in AID_FOR_TRADE_SECTORS:
            raise RuntimeError(f"Unexpected Aid-for-Trade sector {sector!r}")
        identity = (observation.year, flow, sector)
        if identity in by_key:
            raise RuntimeError(
                "Duplicate Aid-for-Trade observation for "
                f"{observation.year}, {flow}, sector {sector}"
            )
        by_key[identity] = observation.value

    totals: list[AidForTradeTotal] = []
    components: list[AidForTradeComponent] = []
    warnings: list[str] = []
    for flow, required_years in required_years_by_flow.items():
        for year in required_years:
            present_sectors = {
                sector
                for source_year, source_flow, sector in by_key
                if source_year == year and source_flow == flow
            }
            if not present_sectors:
                raise RuntimeError(
                    f"No Aid-for-Trade {AID_FOR_TRADE_FLOW_LABELS[flow].lower()} "
                    f"observations exist for {year}"
                )

            total = Decimal(0)
            for sector in AID_FOR_TRADE_SECTORS:
                present = sector in present_sectors
                value = by_key.get((year, flow, sector), Decimal(0))
                if not present:
                    warnings.append(
                        f"{year} {flow} sector {sector} is absent; treated as zero"
                    )
                components.append(
                    AidForTradeComponent(year, flow, sector, value, present)
                )
                total += value
            totals.append(AidForTradeTotal(year, flow, total))

    return AidForTradeResult(
        totals=tuple(sorted(totals, key=lambda item: (item.year, item.flow))),
        components=tuple(
            sorted(components, key=lambda item: (item.year, item.flow, item.sector))
        ),
        warnings=tuple(warnings),
    )


def _values_by_measure(
    observations: Sequence[SdmxObservation], allowed_measures: Sequence[str]
) -> dict[str, dict[int, Decimal]]:
    """Index generic OECD observations by their validated measure code."""

    allowed = set(allowed_measures)
    values = {measure: {} for measure in allowed_measures}
    for observation in observations:
        measure = observation.dimension("MEASURE")
        if measure not in allowed:
            raise RuntimeError(f"Unexpected OECD donor-finance measure {measure!r}")
        if observation.year in values[measure]:
            raise RuntimeError(
                f"Duplicate OECD measure {measure} for {observation.year}"
            )
        values[measure][observation.year] = observation.value
    return values


def calculate_resource_flows(
    dac1_observations: Sequence[SdmxObservation], required_years: Sequence[int]
) -> tuple[ResourceFlowYear, ...]:
    """Select DAC1 measure 5 without applying an unnecessary denominator."""

    values = _values_by_measure(dac1_observations, ("1", "5", "1010"))["5"]
    missing = sorted(set(required_years) - set(values))
    if missing:
        raise RuntimeError(
            "Missing total-resource-flow observations: "
            + ", ".join(map(str, missing))
        )
    return tuple(ResourceFlowYear(year, values[year]) for year in required_years)


def calculate_oda_gni(
    dac1_observations: Sequence[SdmxObservation],
    dac2a_observations: Sequence[SdmxObservation],
    required_years: Sequence[int],
    grant_equivalent_observations: Sequence[SdmxObservation] = (),
) -> tuple[OdaGniYear, ...]:
    """Calculate total and LDC net ODA as percentages of the same U.S. GNI."""

    dac1 = _values_by_measure(dac1_observations, ("1", "5", "1010"))
    dac2a = _values_by_measure(dac2a_observations, ("106", "206"))
    grant_equivalent = (
        _values_by_measure(grant_equivalent_observations, ("11010",))["11010"]
        if grant_equivalent_observations
        else {}
    )

    calculated: list[OdaGniYear] = []
    for year in required_years:
        missing = [
            label
            for label, values in (
                ("GNI (measure 1)", dac1["1"]),
                ("net ODA (measure 1010)", dac1["1010"]),
                ("imputed multilateral LDC ODA (measure 106)", dac2a["106"]),
                ("bilateral/net LDC ODA (measure 206)", dac2a["206"]),
            )
            if year not in values
        ]
        if missing:
            raise RuntimeError(f"{year} is missing " + ", ".join(missing))

        gni = dac1["1"][year]
        if gni <= 0:
            raise RuntimeError(f"Cannot calculate 17.2.1 with non-positive GNI in {year}")
        net_oda = dac1["1010"][year]
        ldc_bilateral = dac2a["206"][year]
        ldc_imputed = dac2a["106"][year]
        calculated.append(
            OdaGniYear(
                year=year,
                net_oda=net_oda,
                gni=gni,
                ldc_bilateral_net_oda=ldc_bilateral,
                ldc_imputed_multilateral_oda=ldc_imputed,
                total_percent=Decimal(100) * net_oda / gni,
                ldc_percent=(
                    Decimal(100) * (ldc_bilateral + ldc_imputed) / gni
                ),
                grant_equivalent_oda=grant_equivalent.get(year),
                grant_equivalent_percent=(
                    Decimal(100) * grant_equivalent[year] / gni
                    if year in grant_equivalent
                    else None
                ),
            )
        )
    return tuple(calculated)


def build_aid_for_trade_standardized(
    calculated: Sequence[AidForTradeTotal], source: OecdResult
) -> list[StandardizedObservation]:
    """Build one standardized row per year and flow, never mixing flows."""

    return [
        StandardizedObservation(
            indicator_id=AID_FOR_TRADE_INDICATOR_ID,
            indicator_title=AID_FOR_TRADE_TITLE,
            year=item.year,
            value=decimal_text(item.value),
            unit=AID_FOR_TRADE_UNIT,
            geography=GEOGRAPHY,
            disaggregation={"flow": AID_FOR_TRADE_FLOW_LABELS[item.flow]},
            source_organization=source.source_organization,
            source_dataset=source.source_dataset,
            source_url=source.source_url,
            retrieval_method=source.retrieval_method,
            retrieval_date=source.retrieval_date,
            methodology_variant=AID_FOR_TRADE_METHODOLOGY,
            validation_status=CURRENT_METHODOLOGY_VERIFIED,
            data_warning=AID_FOR_TRADE_WARNING,
        )
        for item in calculated
    ]


def build_resource_flows_standardized(
    calculated: Sequence[ResourceFlowYear], source: OecdResult
) -> list[StandardizedObservation]:
    """Build the single national 10.b.1 series."""

    return [
        StandardizedObservation(
            indicator_id=RESOURCE_FLOWS_INDICATOR_ID,
            indicator_title=RESOURCE_FLOWS_TITLE,
            year=item.year,
            value=decimal_text(item.value),
            unit=RESOURCE_FLOWS_UNIT,
            geography=GEOGRAPHY,
            disaggregation={},
            source_organization=source.source_organization,
            source_dataset=source.source_dataset,
            source_url=source.source_url,
            retrieval_method=source.retrieval_method,
            retrieval_date=source.retrieval_date,
            methodology_variant=RESOURCE_FLOWS_METHODOLOGY,
            validation_status=CURRENT_METHODOLOGY_VERIFIED,
            data_warning=RESOURCE_FLOWS_WARNING,
        )
        for item in calculated
    ]


def build_oda_gni_standardized(
    calculated: Sequence[OdaGniYear], dac1_source: OecdResult, dac2a_source: OecdResult
) -> list[StandardizedObservation]:
    """Build separately labelled total and LDC components of 17.2.1."""

    if dac1_source.retrieval_date != dac2a_source.retrieval_date:
        raise ValueError("17.2.1 source retrieval dates must match")
    if dac1_source.retrieval_method != dac2a_source.retrieval_method:
        raise ValueError("17.2.1 source retrieval methods must match")

    rows: list[StandardizedObservation] = []
    for item in calculated:
        rows.append(
            StandardizedObservation(
                indicator_id=ODA_GNI_INDICATOR_ID,
                indicator_title=ODA_GNI_TITLE,
                year=item.year,
                value=decimal_text(item.total_percent),
                unit=ODA_GNI_UNIT,
                geography=GEOGRAPHY,
                disaggregation={"component": TOTAL_COMPONENT},
                source_organization=dac1_source.source_organization,
                source_dataset=dac1_source.source_dataset,
                source_url=dac1_source.source_url,
                retrieval_method=dac1_source.retrieval_method,
                retrieval_date=dac1_source.retrieval_date,
                methodology_variant=ODA_GNI_METHODOLOGY,
                validation_status=CURRENT_METHODOLOGY_VERIFIED,
                data_warning=ODA_GNI_WARNING,
            )
        )
        rows.append(
            StandardizedObservation(
                indicator_id=ODA_GNI_INDICATOR_ID,
                indicator_title=ODA_GNI_TITLE,
                year=item.year,
                value=decimal_text(item.ldc_percent),
                unit=ODA_GNI_UNIT,
                geography=GEOGRAPHY,
                disaggregation={"component": LDC_COMPONENT},
                source_organization=dac1_source.source_organization,
                source_dataset=(
                    dac1_source.source_dataset + " | " + dac2a_source.source_dataset
                ),
                source_url=dac1_source.source_url + " | " + dac2a_source.source_url,
                retrieval_method=dac1_source.retrieval_method,
                retrieval_date=dac1_source.retrieval_date,
                methodology_variant=ODA_GNI_METHODOLOGY,
                validation_status=CURRENT_METHODOLOGY_VERIFIED,
                data_warning=ODA_GNI_WARNING,
            )
        )
    return rows


def parse_aid_for_trade_archive(csv_text: str) -> dict[tuple[int, str], Decimal]:
    """Parse archived current-price values, which are already in USD millions."""

    values: dict[tuple[int, str], Decimal] = {}
    for row in csv.DictReader(io.StringIO(csv_text, newline="")):
        try:
            year = int(row["Year"])
            flow_label = row["Measure"].strip()
            flow = {
                "Commitments": "C",
                "Disbursements": "D",
            }[flow_label]
            value = Decimal(row["Value"])
        except (KeyError, ValueError, ArithmeticError) as error:
            raise RuntimeError(f"Invalid archived 8.a.1 row: {row}") from error
        identity = (year, flow)
        if identity in values:
            raise RuntimeError(f"Duplicate archived 8.a.1 row: {identity}")
        values[identity] = value
    return values


def compare_aid_for_trade_archive(
    calculated: Sequence[AidForTradeTotal],
    archived: Mapping[tuple[int, str], Decimal],
) -> list[ArchiveComparison]:
    """Compare unlike price bases diagnostically without requiring equality."""

    current = {(item.year, item.flow): item.value for item in calculated}
    return [
        ArchiveComparison(
            year=year,
            category=AID_FOR_TRADE_FLOW_LABELS[flow],
            archived_value=archived[(year, flow)],
            current_value=current[(year, flow)],
            difference=current[(year, flow)] - archived[(year, flow)],
        )
        for year, flow in sorted(set(current) & set(archived))
    ]


def parse_placeholder_archive(csv_text: str, indicator_id: str) -> PlaceholderArchive:
    """Recognize the exact legacy ``Year,Value / 2015,0`` placeholder."""

    rows = list(csv.DictReader(io.StringIO(csv_text, newline="")))
    if len(rows) != 1:
        raise RuntimeError(f"{indicator_id} archive is not the expected placeholder")
    try:
        year = int(rows[0]["Year"])
        value = Decimal(rows[0]["Value"])
    except (KeyError, ValueError, ArithmeticError) as error:
        raise RuntimeError(f"Invalid archived {indicator_id} placeholder") from error
    if year != 2015 or value != 0:
        raise RuntimeError(f"{indicator_id} archive is not the expected 2015 zero")
    return PlaceholderArchive(year, value)


def _read_archive_text(archive_path: Path, member_path: str) -> str:
    """Read one canonical archive CSV without extracting it."""

    try:
        return read_nested_zip_member(
            archive_path, CANONICAL_ZIP_MEMBER, member_path
        ).decode("utf-8-sig")
    except (ArchiveReadError, UnicodeDecodeError) as error:
        raise RuntimeError(f"Could not read archived canonical file {member_path}") from error


def read_aid_for_trade_archive(archive_path: Path) -> dict[tuple[int, str], Decimal]:
    """Read the canonical archived 8.a.1 CSV in memory."""

    return parse_aid_for_trade_archive(_read_archive_text(archive_path, ARCHIVE_8_A_1))


def read_placeholder_archives(
    archive_path: Path,
) -> tuple[PlaceholderArchive, PlaceholderArchive]:
    """Read and explicitly classify the 10.b.1 and 17.2.1 placeholders."""

    placeholder_10 = parse_placeholder_archive(
        _read_archive_text(archive_path, ARCHIVE_10_B_1), RESOURCE_FLOWS_INDICATOR_ID
    )
    placeholder_17 = parse_placeholder_archive(
        _read_archive_text(archive_path, ARCHIVE_17_2_1), ODA_GNI_INDICATOR_ID
    )
    return placeholder_10, placeholder_17
