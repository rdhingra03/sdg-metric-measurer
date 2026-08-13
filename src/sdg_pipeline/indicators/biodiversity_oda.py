"""Shared transformation for the ODA component of SDG 15.a.1 and 15.b.1.

The two SDG indicators are official repeats.  This module therefore performs
one statistical transformation and can label the resulting observations for
either indicator ID.  It receives parsed OECD observations and knows nothing
about HTTP or SDMX response formats.
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
from ..standardized import (
    CURRENT_METHODOLOGY_VERIFIED,
    StandardizedObservation,
)


INDICATOR_IDS = ("15.a.1", "15.b.1")
INDICATOR_TITLES = {
    "15.a.1": (
        "(a) Official development assistance on conservation and sustainable "
        "use of biodiversity; and (b) revenue generated and finance mobilized "
        "from biodiversity-relevant economic instruments"
    ),
    "15.b.1": (
        "(a) Official development assistance on conservation and sustainable "
        "use of biodiversity; and (b) revenue generated and finance mobilized "
        "from biodiversity-relevant economic instruments"
    ),
}

SIGNIFICANT_SCORE = "1"
PRINCIPAL_SCORE = "2"
ALLOWED_SCORES = (SIGNIFICANT_SCORE, PRINCIPAL_SCORE)
UNIT = "million constant 2024 USD"
GEOGRAPHY = "United States"
METHODOLOGY_VARIANT = "us_donor_biodiversity_oda_commitments"
METHODOLOGY_WARNING = (
    "This is component (a), the ODA component, of the current indicator. It "
    "measures United States donor bilateral-allocable biodiversity-related "
    "ODA commitments in constant 2024 USD, using the full values of principal "
    "and significant Rio Marker flows. It does not measure the separate "
    "biodiversity-relevant economic-instruments component. The legacy U.S. "
    "archive used gross disbursements in current USD, so the current series "
    "intentionally does not reproduce the archived values."
)

ARCHIVED_METHODOLOGY = "gross disbursements, current USD"
CURRENT_METHODOLOGY = (
    "bilateral-allocable commitments, principal + significant biodiversity "
    "Rio Marker flows, constant 2024 USD"
)
CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
ARCHIVE_DATA_PATHS = {
    "15.a.1": "sdg-master/data/indicator_15-a-1.csv",
    "15.b.1": "sdg-master/data/indicator_15-b-1.csv",
}


@dataclass(frozen=True)
class BiodiversityOdaYear:
    """Auditable principal, significant, and combined values for one year."""

    year: int
    principal: Decimal
    significant: Decimal
    combined: Decimal


@dataclass(frozen=True)
class CalculationResult:
    """Calculated annual values plus non-fatal source-shape warnings."""

    years: tuple[BiodiversityOdaYear, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveComparison:
    """One diagnostic comparison across a deliberate methodology break."""

    year: int
    archived_value: Decimal
    current_value: Decimal
    difference: Decimal
    archived_methodology: str = ARCHIVED_METHODOLOGY
    current_methodology: str = CURRENT_METHODOLOGY


def decimal_text(value: Decimal) -> str:
    """Return ordinary decimal notation while preserving available precision."""

    return format(value, "f")


def calculate(
    observations: Sequence[SdmxObservation],
    required_years: Sequence[int],
) -> CalculationResult:
    """Add full principal and significant values once for every year.

    OECD omits aggregate cells whose value is zero.  If one of the two scores
    is absent while the other score exists for a required year, the missing
    score is recorded as zero and a warning is retained.  If both are absent,
    the year is genuinely missing and calculation fails.
    """

    values: dict[int, dict[str, Decimal]] = {}
    for observation in observations:
        score = observation.dimension("SCORE")
        if score not in ALLOWED_SCORES:
            raise RuntimeError(
                f"Biodiversity ODA received excluded OECD score {score!r}"
            )
        year_values = values.setdefault(observation.year, {})
        if score in year_values:
            raise RuntimeError(
                f"Duplicate biodiversity score {score} for {observation.year}"
            )
        year_values[score] = observation.value

    warnings: list[str] = []
    calculated: list[BiodiversityOdaYear] = []
    for year in required_years:
        year_values = values.get(year)
        if not year_values:
            raise RuntimeError(f"No biodiversity ODA observations exist for {year}")
        for score in ALLOWED_SCORES:
            if score not in year_values:
                label = "significant" if score == SIGNIFICANT_SCORE else "principal"
                warnings.append(
                    f"{year} has no OECD {label} aggregate; treated as zero"
                )
                year_values[score] = Decimal(0)

        principal = year_values[PRINCIPAL_SCORE]
        significant = year_values[SIGNIFICANT_SCORE]
        calculated.append(
            BiodiversityOdaYear(
                year=year,
                principal=principal,
                significant=significant,
                combined=principal + significant,
            )
        )
    return CalculationResult(tuple(calculated), tuple(warnings))


def build_standardized_observations(
    indicator_id: str,
    calculated: Sequence[BiodiversityOdaYear],
    source: OecdResult,
) -> list[StandardizedObservation]:
    """Label one shared series for either repeat indicator."""

    if indicator_id not in INDICATOR_IDS:
        raise ValueError(f"Unsupported biodiversity ODA indicator: {indicator_id}")
    return [
        StandardizedObservation(
            indicator_id=indicator_id,
            indicator_title=INDICATOR_TITLES[indicator_id],
            year=item.year,
            value=decimal_text(item.combined),
            unit=UNIT,
            geography=GEOGRAPHY,
            disaggregation={},
            source_organization=source.source_organization,
            source_dataset=source.source_dataset,
            source_url=source.source_url,
            retrieval_method=source.retrieval_method,
            retrieval_date=source.retrieval_date,
            methodology_variant=METHODOLOGY_VARIANT,
            validation_status=CURRENT_METHODOLOGY_VERIFIED,
            data_warning=METHODOLOGY_WARNING,
        )
        for item in calculated
    ]


def parse_archived_values(csv_text: str) -> dict[int, Decimal]:
    """Parse nominal archived dollars and convert them to USD millions."""

    values: dict[int, Decimal] = {}
    for row in csv.DictReader(io.StringIO(csv_text, newline="")):
        try:
            year = int(row["Year"])
            value = Decimal(row["Value"]) / Decimal(1_000_000)
        except (KeyError, ValueError, ArithmeticError) as error:
            raise RuntimeError(f"Invalid archived biodiversity ODA row: {row}") from error
        units = (row.get("Units") or "").strip()
        if units != "Gross Disbursements, USD Current":
            raise RuntimeError(f"Unexpected archived biodiversity ODA units: {units!r}")
        if year in values:
            raise RuntimeError(f"Duplicate archived biodiversity ODA year: {year}")
        values[year] = value
    return values


def read_archived_values(archive_path: Path) -> dict[int, Decimal]:
    """Read both repeat-indicator files and require them to remain identical."""

    parsed: dict[str, dict[int, Decimal]] = {}
    try:
        for indicator_id, member_path in ARCHIVE_DATA_PATHS.items():
            csv_text = read_nested_zip_member(
                archive_path, CANONICAL_ZIP_MEMBER, member_path
            ).decode("utf-8-sig")
            parsed[indicator_id] = parse_archived_values(csv_text)
    except (ArchiveReadError, UnicodeDecodeError) as error:
        raise RuntimeError(
            "Could not read the archived canonical biodiversity ODA CSVs"
        ) from error

    if parsed["15.a.1"] != parsed["15.b.1"]:
        raise RuntimeError("Archived 15.a.1 and 15.b.1 values unexpectedly differ")
    return parsed["15.a.1"]


def compare_with_archive(
    calculated: Sequence[BiodiversityOdaYear],
    archived: Mapping[int, Decimal],
) -> list[ArchiveComparison]:
    """Build a diagnostic comparison without requiring unlike methods to match."""

    current = {item.year: item.combined for item in calculated}
    overlap = sorted(set(current) & set(archived))
    return [
        ArchiveComparison(
            year=year,
            archived_value=archived[year],
            current_value=current[year],
            difference=current[year] - archived[year],
        )
        for year in overlap
    ]
