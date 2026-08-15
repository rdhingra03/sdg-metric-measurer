"""Current U.S. NVSS implementation of SDG indicator 3.1.2."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from ..sources.nvss_natality import NatalityObservation, NvssNatalityResult
from ..standardized import (
    ARCHIVE_MATCHED,
    CURRENT_METHODOLOGY_VERIFIED,
    StandardizedObservation,
)


INDICATOR_ID = "3.1.2"
INDICATOR_TITLE = "Proportion of births attended by skilled health personnel"
METHODOLOGY_VARIANT = "nvss_birth_attendant_current_categories"
UNIT = "percent"

# Current expanded NVSS medical-attendant categories.  Codes 1-4 correspond
# to MD, DO, certified nurse-midwife / certified midwife (including the
# documented advanced-practice-nurse grouping), and other midwife.
SKILLED_ATTENDANT_CODES = frozenset({"1", "2", "3", "4"})
EXPECTED_ATTENDANT_CODES = frozenset({"1", "2", "3", "4", "5", "9"})
DATA_WARNING = (
    "Current NVSS medical-attendant categories 1-4 are included. The broad "
    "'Other Midwife' category is retained for continuity with the archived "
    "U.S. method, but the aggregate source does not independently verify each "
    "attendant's credentialing. Current final data may revise an older archive vintage."
)


@dataclass(frozen=True)
class SkilledAttendanceYear:
    year: int
    total_births: int
    skilled_births: int
    included_categories: tuple[str, ...]
    percentage: Decimal


def calculate(rows: Sequence[NatalityObservation]) -> list[SkilledAttendanceYear]:
    """Aggregate all attendant categories into one national annual percentage."""

    by_year: dict[int, list[NatalityObservation]] = {}
    for row in rows:
        by_year.setdefault(row.year, []).append(row)
    results: list[SkilledAttendanceYear] = []
    for year, year_rows in sorted(by_year.items()):
        codes = [row.category_code for row in year_rows]
        if len(codes) != len(set(codes)):
            raise RuntimeError(f"SDG 3.1.2 has duplicate attendant categories in {year}")
        if set(codes) != EXPECTED_ATTENDANT_CODES:
            missing = sorted(EXPECTED_ATTENDANT_CODES - set(codes))
            extra = sorted(set(codes) - EXPECTED_ATTENDANT_CODES)
            raise RuntimeError(
                f"SDG 3.1.2 attendant categories are incomplete in {year}; "
                f"missing={missing}, extra={extra}"
            )
        if any(
            row.suppression_status != "not_suppressed" or row.births is None
            for row in year_rows
        ):
            raise RuntimeError(f"SDG 3.1.2 cannot publish suppressed births in {year}")
        births_by_code = {row.category_code: row.births or 0 for row in year_rows}
        total = sum(births_by_code.values())
        skilled = sum(births_by_code[code] for code in SKILLED_ATTENDANT_CODES)
        if total <= 0:
            raise RuntimeError(f"SDG 3.1.2 requires positive total births in {year}")
        results.append(
            SkilledAttendanceYear(
                year=year,
                total_births=total,
                skilled_births=skilled,
                included_categories=tuple(
                    row.category_label
                    for row in year_rows
                    if row.category_code in SKILLED_ATTENDANT_CODES
                ),
                percentage=Decimal(100) * Decimal(skilled) / Decimal(total),
            )
        )
    if not results:
        raise RuntimeError("SDG 3.1.2 received no natality observations")
    return results


def decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def archive_matches(value: Decimal, archived: Decimal) -> bool:
    """Compare at the decimal precision stored in the canonical archive."""

    quantum = Decimal(1).scaleb(archived.as_tuple().exponent)
    return value.quantize(quantum) == archived


def build_standardized(
    values: Sequence[SkilledAttendanceYear],
    source: NvssNatalityResult,
    archived: Mapping[int, Decimal],
) -> list[StandardizedObservation]:
    return [
        StandardizedObservation(
            indicator_id=INDICATOR_ID,
            indicator_title=INDICATOR_TITLE,
            year=item.year,
            value=decimal_text(item.percentage),
            unit=UNIT,
            geography="United States",
            disaggregation={},
            source_organization=source.source_organization,
            source_dataset=source.source_dataset,
            source_url=source.births_source_url or source.source_url,
            retrieval_method=source.retrieval_method,
            retrieval_date=source.retrieval_date,
            methodology_variant=METHODOLOGY_VARIANT,
            validation_status=(
                ARCHIVE_MATCHED
                if item.year in archived and archive_matches(item.percentage, archived[item.year])
                else CURRENT_METHODOLOGY_VERIFIED
            ),
            data_warning=DATA_WARNING,
        )
        for item in values
    ]
