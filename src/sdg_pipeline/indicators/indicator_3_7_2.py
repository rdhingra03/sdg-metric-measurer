"""Current U.S. NVSS implementation of SDG indicator 3.7.2."""

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


INDICATOR_ID = "3.7.2"
INDICATOR_TITLE = (
    "Adolescent birth rate (aged 10-14 years; aged 15-19 years) "
    "per 1,000 women in that age group"
)
METHODOLOGY_VARIANT = "nvss_age_specific_fertility_rate"
UNIT = "births per 1,000 women"
AGE_CODE_TO_GROUP = {"15": "10-14", "15-19": "15-19"}
DATA_WARNING = (
    "Final resident live births are divided by the official Census "
    "female-population denominator for each maternal-age group. Current final "
    "data may revise the older archived vintage; birth counts of 1-9 are suppressed."
)


@dataclass(frozen=True)
class AdolescentBirthRateYear:
    year: int
    age_group: str
    births: int
    female_population: int
    rate: Decimal
    source_reported_rate: Decimal | None


def calculate(rows: Sequence[NatalityObservation]) -> list[AdolescentBirthRateYear]:
    """Calculate both official rates from births and female denominators."""

    seen: set[tuple[int, str]] = set()
    results: list[AdolescentBirthRateYear] = []
    for row in sorted(rows, key=lambda item: (item.year, item.category_code)):
        if row.category_code not in AGE_CODE_TO_GROUP:
            raise RuntimeError(
                f"SDG 3.7.2 received unexpected age code {row.category_code!r}"
            )
        age_group = AGE_CODE_TO_GROUP[row.category_code]
        key = (row.year, age_group)
        if key in seen:
            raise RuntimeError(f"SDG 3.7.2 has duplicate row {row.year}/{age_group}")
        seen.add(key)
        if row.suppression_status != "not_suppressed":
            raise RuntimeError(f"SDG 3.7.2 cannot publish suppressed row {key}")
        if row.births is None or row.births < 0:
            raise RuntimeError(f"SDG 3.7.2 requires a valid birth count for {key}")
        if row.female_population is None or row.female_population <= 0:
            raise RuntimeError(
                f"SDG 3.7.2 requires a positive female population for {key}"
            )
        rate = Decimal(1000) * Decimal(row.births) / Decimal(row.female_population)
        results.append(
            AdolescentBirthRateYear(
                year=row.year,
                age_group=age_group,
                births=row.births,
                female_population=row.female_population,
                rate=rate,
                source_reported_rate=row.source_reported_fertility_rate,
            )
        )
    years = {item.year for item in results}
    for year in years:
        groups = {item.age_group for item in results if item.year == year}
        if groups != {"10-14", "15-19"}:
            raise RuntimeError(f"SDG 3.7.2 is missing an age group in {year}")
    if not results:
        raise RuntimeError("SDG 3.7.2 received no natality observations")
    return results


def decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def archive_matches(value: Decimal, archived: Decimal) -> bool:
    quantum = Decimal(1).scaleb(archived.as_tuple().exponent)
    return value.quantize(quantum) == archived


def build_standardized(
    values: Sequence[AdolescentBirthRateYear],
    source: NvssNatalityResult,
    archived: Mapping[tuple[int, str], Decimal],
) -> list[StandardizedObservation]:
    return [
        StandardizedObservation(
            indicator_id=INDICATOR_ID,
            indicator_title=INDICATOR_TITLE,
            year=item.year,
            value=decimal_text(item.rate),
            unit=UNIT,
            geography="United States",
            disaggregation={"age": item.age_group},
            source_organization=source.source_organization + " / U.S. Census Bureau",
            source_dataset=source.source_dataset + " with Census population denominators",
            source_url=" | ".join(
                value
                for value in (
                    source.births_source_url,
                    source.population_source_url,
                )
                if value
            )
            or source.source_url,
            retrieval_method=source.retrieval_method,
            retrieval_date=source.retrieval_date,
            methodology_variant=METHODOLOGY_VARIANT,
            validation_status=(
                ARCHIVE_MATCHED
                if (item.year, item.age_group) in archived
                and archive_matches(item.rate, archived[(item.year, item.age_group)])
                else CURRENT_METHODOLOGY_VERIFIED
            ),
            data_warning=DATA_WARNING,
        )
        for item in values
    ]
