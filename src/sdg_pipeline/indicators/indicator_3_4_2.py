"""Current-methodology U.S. implementation of SDG 3.4.2."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from ..sources.nvss_mortality import (
    MortalityObservation,
    NvssMortalityResult,
    calculate_crude_rate,
)
from ..standardized import CURRENT_METHODOLOGY_VERIFIED, StandardizedObservation


INDICATOR_ID = "3.4.2"
INDICATOR_TITLE = "Suicide mortality rate"
ICD10_SELECTION = ("X60-X84", "Y87.0")
METHODOLOGY_VARIANT = "nvss_crude_suicide_mortality"
UNIT = "deaths per 100,000 population"
DATA_WARNING = (
    "Current methodology: crude national rate using ICD-10 X60-X84 and Y87.0. "
    "The archived U.S. series is age-adjusted, so archive differences are "
    "diagnostic and are not validation failures."
)


def calculate(observation: MortalityObservation) -> Decimal:
    """Calculate the current UN crude rate from exact NVSS counts."""

    if observation.icd10_selection != ICD10_SELECTION:
        raise RuntimeError("SDG 3.4.2 received the wrong ICD-10 selection")
    if observation.suppression_status != "not_suppressed":
        raise RuntimeError("SDG 3.4.2 cannot publish a suppressed observation")
    if observation.deaths is None or observation.population is None:
        raise RuntimeError("SDG 3.4.2 requires deaths and population")
    return calculate_crude_rate(observation.deaths, observation.population)


def decimal_text(value: Decimal) -> str:
    """Keep useful precision while avoiding unwieldy repeating decimals."""

    return format(value.quantize(Decimal("0.000001")), "f")


def build_standardized(
    rows: Sequence[MortalityObservation], source: NvssMortalityResult
) -> list[StandardizedObservation]:
    """Map connector observations to the project's 15-column schema."""

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
