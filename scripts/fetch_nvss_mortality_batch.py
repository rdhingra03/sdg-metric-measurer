#!/usr/bin/env python3
"""Fetch the current-methodology NCHS/NVSS mortality SDG batch.

This coordinated run produces SDG 3.4.2, 3.6.1, and 3.9.3 from final
national mortality counts in CDC WONDER.  The canonical archive is read only
for a diagnostic comparison: its rates are age-adjusted (and 3.9.3 used an
older, broader ICD selection), while the current UN indicators require crude
rates.  Archive equality is therefore neither expected nor required.
"""

from __future__ import annotations

import csv
import io
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sdg_pipeline.archive import ArchiveReadError, read_nested_zip_member
from sdg_pipeline.errors import RetrievalError
from sdg_pipeline.indicators import indicator_3_4_2
from sdg_pipeline.indicators import indicator_3_6_1
from sdg_pipeline.indicators import indicator_3_9_3
from sdg_pipeline.output import current_retrieval_date, write_csv_outputs_atomically
from sdg_pipeline.sources import nvss_mortality
from sdg_pipeline.standardized import STANDARDIZED_COLUMNS, observation_to_row


ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"

INDICATOR_MODULES = {
    "3.4.2": indicator_3_4_2,
    "3.6.1": indicator_3_6_1,
    "3.9.3": indicator_3_9_3,
}
STANDARDIZED_PATHS = {
    indicator_id: PROJECT_ROOT
    / "data_processed"
    / "standardized"
    / f"sdg_{indicator_id.replace('.', '_')}.csv"
    for indicator_id in INDICATOR_MODULES
}
AUDIT_PATHS = {
    indicator_id: PROJECT_ROOT
    / "data_processed"
    / "audit"
    / f"sdg_{indicator_id.replace('.', '_')}_mortality_inputs.csv"
    for indicator_id in INDICATOR_MODULES
}
AUDIT_COLUMNS = [
    "indicator_id",
    "year",
    "icd10_selection",
    "deaths",
    "population",
    "calculated_crude_rate",
    "source_reported_crude_rate",
    "source_url",
    "retrieval_method",
    "retrieval_date",
    "suppression_status",
    "source_notes",
    "archive_value",
    "archive_methodology",
    "current_methodology",
    "difference_from_archive",
]


def read_archived_values(indicator_id: str) -> dict[int, Decimal]:
    """Read one canonical archive CSV without extracting either archive."""

    member = f"sdg-master/data/indicator_{indicator_id.replace('.', '-')}.csv"
    try:
        text = read_nested_zip_member(
            ARCHIVE_PATH, CANONICAL_ZIP_MEMBER, member
        ).decode("utf-8-sig")
    except (ArchiveReadError, UnicodeDecodeError) as error:
        raise RuntimeError(
            f"Could not read canonical archived SDG {indicator_id} data"
        ) from error

    values: dict[int, Decimal] = {}
    for row in csv.DictReader(io.StringIO(text, newline="")):
        try:
            year = int(row["Year"])
            value = Decimal(row["Value"])
        except (KeyError, ValueError, InvalidOperation) as error:
            raise RuntimeError(
                f"Invalid archived SDG {indicator_id} row: {row}"
            ) from error
        if year in values:
            raise RuntimeError(f"Duplicate archived SDG {indicator_id} year {year}")
        values[year] = value
    if not values:
        raise RuntimeError(f"Archived SDG {indicator_id} contains no values")
    return values


def build_queries() -> list[nvss_mortality.MortalityQuery]:
    """Describe the three suppression-safe aggregate ICD requests."""

    return [
        nvss_mortality.MortalityQuery(
            key=indicator_id,
            icd10_selection=module.ICD10_SELECTION,
        )
        for indicator_id, module in INDICATOR_MODULES.items()
    ]


def archive_methodology(indicator_id: str) -> str:
    """Describe why the archive is not an equality baseline."""

    if indicator_id == "3.9.3":
        return "age-adjusted rate using the older, broader ICD-10 X40-X49 selection"
    return "age-adjusted mortality rate"


def current_methodology(indicator_id: str) -> str:
    module = INDICATOR_MODULES[indicator_id]
    return f"crude rate using ICD-10 {module.ICD10_SELECTION}"


def audit_rows(
    indicator_id: str,
    observations: tuple[nvss_mortality.MortalityObservation, ...],
    source: nvss_mortality.NvssMortalityResult,
    archived: Mapping[int, Decimal],
) -> list[dict[str, object]]:
    """Preserve exact source inputs and methodology-aware archive diagnostics."""

    module = INDICATOR_MODULES[indicator_id]
    rows: list[dict[str, object]] = []
    for observation in observations:
        calculated = module.calculate(observation)
        archived_value = archived.get(observation.year)
        difference = calculated - archived_value if archived_value is not None else None
        rows.append(
            {
                "indicator_id": indicator_id,
                "year": observation.year,
                "icd10_selection": "; ".join(observation.icd10_selection),
                "deaths": observation.deaths if observation.deaths is not None else "",
                "population": (
                    observation.population if observation.population is not None else ""
                ),
                "calculated_crude_rate": module.decimal_text(calculated),
                "source_reported_crude_rate": (
                    format(observation.source_reported_crude_rate, "f")
                    if observation.source_reported_crude_rate is not None
                    else ""
                ),
                "source_url": source.source_url,
                "retrieval_method": source.retrieval_method,
                "retrieval_date": source.retrieval_date,
                "suppression_status": observation.suppression_status,
                "source_notes": " | ".join(
                    (*observation.source_notes, *source.source_warnings)
                ),
                "archive_value": (
                    format(archived_value, "f") if archived_value is not None else ""
                ),
                "archive_methodology": archive_methodology(indicator_id),
                "current_methodology": current_methodology(indicator_id),
                "difference_from_archive": (
                    module.decimal_text(difference) if difference is not None else ""
                ),
            }
        )
    return rows


def diagnostic_summary(
    observations: tuple[nvss_mortality.MortalityObservation, ...],
    archived: Mapping[int, Decimal],
    module: object,
) -> dict[str, object]:
    """Summarize overlapping archive comparisons without asserting equality."""

    by_year = {observation.year: observation for observation in observations}
    overlap = sorted(set(by_year) & set(archived))
    differences = {
        year: module.calculate(by_year[year]) - archived[year] for year in overlap
    }
    return {
        "overlapping_years": len(overlap),
        "latest_overlap_year": overlap[-1] if overlap else None,
        "latest_archive_value": archived[overlap[-1]] if overlap else None,
        "latest_current_value": (
            module.calculate(by_year[overlap[-1]]) if overlap else None
        ),
        "latest_difference": differences.get(overlap[-1]) if overlap else None,
        "maximum_absolute_difference": (
            max(abs(value) for value in differences.values()) if differences else None
        ),
    }


def run() -> dict[str, object]:
    """Retrieve, calculate, compare, and atomically publish the full batch."""

    retrieval_date = current_retrieval_date()
    source = nvss_mortality.fetch_mortality_batch(
        build_queries(), retrieval_date=retrieval_date
    )

    outputs = []
    summaries: dict[str, object] = {}
    for indicator_id, module in INDICATOR_MODULES.items():
        observations = source.observations[indicator_id]
        archived = read_archived_values(indicator_id)
        standardized = module.build_standardized(observations, source)
        audit = audit_rows(indicator_id, observations, source, archived)
        outputs.extend(
            [
                (
                    STANDARDIZED_PATHS[indicator_id],
                    STANDARDIZED_COLUMNS,
                    [observation_to_row(row) for row in standardized],
                ),
                (AUDIT_PATHS[indicator_id], AUDIT_COLUMNS, audit),
            ]
        )
        latest = observations[-1]
        summaries[indicator_id] = {
            "years_retrieved": len(observations),
            "first_year": observations[0].year,
            "latest_year": latest.year,
            "latest_deaths": latest.deaths,
            "latest_population": latest.population,
            "latest_crude_rate": module.calculate(latest),
            "archive_diagnostic": diagnostic_summary(
                observations, archived, module
            ),
        }

    # All six files are fully prepared before any prior successful output is
    # replaced. A retrieval/calculation failure occurs before this point.
    write_csv_outputs_atomically(outputs)
    return {
        "source_method": source.retrieval_method,
        "source_url": source.source_url,
        "source_warnings": source.source_warnings,
        "indicators": summaries,
    }


def main() -> int:
    try:
        result = run()
    except (RetrievalError, RuntimeError, OSError, ValueError) as error:
        print(f"SDG NVSS mortality batch failed: {error}", file=sys.stderr)
        return 1

    print(f"Source method: {result['source_method']}")
    print(f"Source URL: {result['source_url']}")
    for warning in result["source_warnings"]:
        print(f"Source warning: {warning}")
    for indicator_id, summary in result["indicators"].items():
        diagnostic = summary["archive_diagnostic"]
        print(
            f"SDG {indicator_id}: {summary['first_year']}-"
            f"{summary['latest_year']} ({summary['years_retrieved']} years); "
            f"latest deaths={summary['latest_deaths']:,}, "
            f"population={summary['latest_population']:,}, "
            f"crude rate={summary['latest_crude_rate']:.6f}"
        )
        print(
            "  Archive diagnostic: "
            f"{diagnostic['overlapping_years']} overlapping years; latest "
            f"{diagnostic['latest_overlap_year']} archive="
            f"{diagnostic['latest_archive_value']} current="
            f"{diagnostic['latest_current_value']:.6f} difference="
            f"{diagnostic['latest_difference']:.6f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
