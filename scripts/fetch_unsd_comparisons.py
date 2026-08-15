#!/usr/bin/env python3
"""Build reviewed UN comparison files for the 13 automated U.S. indicators.

The outputs are deliberately separate from ``data_processed/standardized``.
They can inform comparisons or an explicitly reviewed fallback, but they never
overwrite a calculated U.S. observation.  In particular, a temporary U.S.
pipeline failure must retain the last successful U.S. output rather than cause
this comparison layer to substitute a UN value.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sdg_pipeline.errors import RetrievalError
from sdg_pipeline.output import write_csv_outputs_atomically
from sdg_pipeline.sources import unsd
from sdg_pipeline import unsd_comparison as comparison


COMPARISON_DIR = PROJECT_ROOT / "data_processed" / "comparison"
INDICATOR_IDS = comparison.indicator_ids()


def output_path(indicator_id: str, output_dir: Path = COMPARISON_DIR) -> Path:
    """Return the deterministic comparison filename for one indicator."""

    return output_dir / f"sdg_{indicator_id.replace('.', '_')}.csv"


def write_outputs(
    rows_by_indicator: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    output_dir: Path = COMPARISON_DIR,
) -> None:
    """Validate all 13 outputs before atomically replacing any CSV."""

    expected = set(INDICATOR_IDS)
    actual = set(rows_by_indicator)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing indicators: " + ", ".join(missing))
        if extra:
            details.append("unexpected indicators: " + ", ".join(extra))
        raise ValueError("Invalid UNSD comparison output set (" + "; ".join(details) + ")")

    outputs = []
    for indicator_id in INDICATOR_IDS:
        rows = [
            comparison.validate_comparison_row(row)
            for row in rows_by_indicator[indicator_id]
        ]
        if not rows:
            raise ValueError(f"Comparison output for {indicator_id} is empty")
        outputs.append(
            (
                output_path(indicator_id, output_dir),
                comparison.COMPARISON_COLUMNS,
                rows,
            )
        )
    write_csv_outputs_atomically(outputs)


def retrieve_and_build() -> tuple[unsd.UnsdResult, dict[str, list[dict[str, object]]]]:
    """Retrieve one U.S. bulk slice and apply the reviewed selection rules."""

    result = unsd.fetch_indicator_observations(
        INDICATOR_IDS,
        area_code=unsd.UNITED_STATES_M49_CODE,
    )
    return result, comparison.build_comparison_rows(result)


def _preferred_rows(
    rows: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    return [row for row in rows if row["is_preferred_comparison"] == "true"]


def print_summary(
    result: unsd.UnsdResult,
    rows_by_indicator: Mapping[str, Sequence[Mapping[str, object]]],
) -> None:
    """Print a concise audit of the generated comparison layer."""

    print(f"Raw UNSD observations: {result.raw_observation_count}")
    print(f"Deduplicated UNSD observations: {result.deduplicated_observation_count}")
    print(f"Database release: {result.database_release}")
    print(f"Database last updated: {result.database_last_updated}")
    for warning in result.warnings:
        print(f"Warning: {warning}")
    for indicator_id in INDICATOR_IDS:
        rows = rows_by_indicator[indicator_id]
        selected = _preferred_rows(rows)
        rendered = "; ".join(
            f"{row['comparison_component']}={row['value']} ({row['year']})"
            for row in selected
        )
        incomplete = sum(
            row["completeness_status"] in {"apparently_incomplete", "provisional"}
            for row in rows
        )
        first = rows[0]
        print(
            f"{indicator_id}: {len(rows)} rows; {rendered}; "
            f"{first['comparison_status']}; {first['fallback_suitability']}; "
            f"flagged={incomplete}"
        )


def main() -> None:
    try:
        result, rows_by_indicator = retrieve_and_build()
        write_outputs(rows_by_indicator)
    except RetrievalError as error:
        raise SystemExit(f"UNSD comparison retrieval failed: {error}") from error
    print_summary(result, rows_by_indicator)


if __name__ == "__main__":
    main()
