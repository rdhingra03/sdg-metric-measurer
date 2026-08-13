"""Current-methodology U.S. implementation of SDG 3.6.1."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from ..sources.nvss_mortality import (
    MortalityObservation,
    NvssMortalityResult,
    calculate_crude_rate,
)
from ..standardized import CURRENT_METHODOLOGY_VERIFIED, StandardizedObservation


INDICATOR_ID = "3.6.1"
INDICATOR_TITLE = "Death rate due to road traffic injuries"

# The UN metadata expresses many inclusive ranges in compact notation.  CDC's
# advanced finder accepts individual valid four-character codes, not those
# compact ranges.  Expanding only codes that exist in the WHO mortality
# classification preserves the exact definition without accidentally adding
# invalid in-between values.
def _v_codes(categories: range | tuple[int, ...], suffixes: tuple[int, ...]):
    return tuple(
        f"V{category:02d}.{suffix}"
        for category in categories
        for suffix in suffixes
    )


ICD10_SELECTION = (
    *_v_codes((1, 2, 3, 4, 6), (1, 9)),
    *_v_codes((9,), (2, 3)),
    *_v_codes(range(10, 15), (3, 4, 5, 9)),
    *_v_codes(range(15, 19), (4, 5, 9)),
    *_v_codes((19,), (4, 5, 6, 8, 9)),
    *_v_codes(range(20, 29), (3, 4, 5, 9)),
    *_v_codes((29,), (4, 5, 6, 8, 9)),
    *_v_codes(range(30, 39), (4, 5, 6, 7, 9)),
    *_v_codes((39,), (4, 5, 6, 8, 9)),
    *_v_codes(range(40, 49), (4, 5, 6, 7, 9)),
    *_v_codes((49,), (4, 5, 6, 8, 9)),
    *_v_codes(range(50, 59), (4, 5, 6, 7, 9)),
    *_v_codes((59,), (4, 5, 6, 8, 9)),
    *_v_codes(range(60, 69), (4, 5, 6, 7, 9)),
    *_v_codes((69,), (4, 5, 6, 8, 9)),
    *_v_codes(range(70, 79), (4, 5, 6, 7, 9)),
    *_v_codes((79,), (4, 5, 6, 8, 9)),
    *_v_codes((80,), (3, 4, 5)),
    "V81.1", "V82.1", "V82.8", "V82.9",
    *_v_codes(range(83, 87), (0, 1, 2, 3)),
    *_v_codes((87,), tuple(range(10))),
    "V89.2", "V89.3", "V89.9", "V99", "Y85.0",
)

# X59.4 is present in the current UN metadata but is not a valid selectable
# code in the current CDC D158 mortality classification.  It therefore has no
# U.S. records to add; this source-system limitation is disclosed in output.
UN_CODE_UNAVAILABLE_IN_WONDER = "X59.4"
METHODOLOGY_VARIANT = "nvss_crude_road_traffic_mortality"
UNIT = "deaths per 100,000 population"
DATA_WARNING = (
    "Current methodology: crude national rate using the current UN four-"
    "character road-traffic ICD-10 selection. UN-listed X59.4 is not a valid "
    "selectable code in CDC WONDER D158 and contributes no U.S. records. The "
    "archived U.S. series is age-adjusted, so archive differences are "
    "diagnostic only."
)


def calculate(observation: MortalityObservation) -> Decimal:
    """Calculate the current UN crude rate from exact NVSS counts."""

    if observation.icd10_selection != ICD10_SELECTION:
        raise RuntimeError("SDG 3.6.1 received the wrong ICD-10 selection")
    if observation.suppression_status != "not_suppressed":
        raise RuntimeError("SDG 3.6.1 cannot publish a suppressed observation")
    if observation.deaths is None or observation.population is None:
        raise RuntimeError("SDG 3.6.1 requires deaths and population")
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
