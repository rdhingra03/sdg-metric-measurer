"""Current-methodology U.S. implementation of SDG 3.9.3."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from ..sources.nvss_mortality import (
    MortalityObservation,
    NvssMortalityResult,
    calculate_crude_rate,
)
from ..standardized import CURRENT_METHODOLOGY_VERIFIED, StandardizedObservation


INDICATOR_ID = "3.9.3"
INDICATOR_TITLE = "Mortality rate attributed to unintentional poisoning"
ICD10_SELECTION = ("X40", "X43", "X46", "X47", "X48", "X49")
EXCLUDED_ARCHIVE_ONLY_CODES = ("X41", "X42", "X44", "X45")
METHODOLOGY_VARIANT = "nvss_crude_unintentional_poisoning_mortality"
UNIT = "deaths per 100,000 population"
DATA_WARNING = (
    "Current methodology: crude national rate using ICD-10 X40, X43, and "
    "X46-X49 (sent to WONDER as four explicit categories). The archived U.S. "
    "series is age-adjusted and used broader "
    "X40-X49, so archive differences are diagnostic only."
)


def calculate(observation: MortalityObservation) -> Decimal:
    """Calculate the current UN crude rate from exact NVSS counts."""

    if observation.icd10_selection != ICD10_SELECTION:
        raise RuntimeError("SDG 3.9.3 received the wrong ICD-10 selection")
    if any(code in observation.icd10_selection for code in EXCLUDED_ARCHIVE_ONLY_CODES):
        raise RuntimeError("SDG 3.9.3 received archive-only poisoning codes")
    if observation.suppression_status != "not_suppressed":
        raise RuntimeError("SDG 3.9.3 cannot publish a suppressed observation")
    if observation.deaths is None or observation.population is None:
        raise RuntimeError("SDG 3.9.3 requires deaths and population")
    return calculate_crude_rate(observation.deaths, observation.population)


def decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def build_standardized(
    rows: Sequence[MortalityObservation], source: NvssMortalityResult
) -> list[StandardizedObservation]:
    return [
        StandardizedObservation(
            indicator_id=INDICATOR_ID,
            indicator_title=INDICATOR_TITLE,
            year=row.year,
            value=decimal_text(calculate(row)),
            unit=UNIT,
            geography="United States",
            disaggregation=row.disaggregation,
            source_organization=source.source_organization,
            source_dataset=source.source_dataset,
            source_url=source.source_url,
            retrieval_method=source.retrieval_method,
            retrieval_date=source.retrieval_date,
            methodology_variant=METHODOLOGY_VARIANT,
            validation_status=CURRENT_METHODOLOGY_VERIFIED,
            data_warning=DATA_WARNING,
        )
        for row in rows
    ]
