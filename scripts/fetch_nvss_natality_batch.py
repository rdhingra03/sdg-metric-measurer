#!/usr/bin/env python3
"""Fetch and publish the current NVSS natality SDG batch.

One coordinated CDC WONDER retrieval produces SDG 3.1.2 and 3.7.2.  The
canonical legacy archive is read in place only for stored-precision comparison;
it is never extracted or modified.  All four output CSVs are prepared before
any previous successful output is replaced.
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
from sdg_pipeline.indicators import indicator_3_1_2, indicator_3_7_2
from sdg_pipeline.output import current_retrieval_date, write_csv_outputs_atomically
from sdg_pipeline.sources import nvss_natality
from sdg_pipeline.standardized import STANDARDIZED_COLUMNS, observation_to_row


ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
STANDARDIZED_PATHS = {
    "3.1.2": PROJECT_ROOT / "data_processed/standardized/sdg_3_1_2.csv",
    "3.7.2": PROJECT_ROOT / "data_processed/standardized/sdg_3_7_2.csv",
}
AUDIT_PATHS = {
    "3.1.2": PROJECT_ROOT / "data_processed/audit/sdg_3_1_2_natality_inputs.csv",
    "3.7.2": PROJECT_ROOT / "data_processed/audit/sdg_3_7_2_natality_inputs.csv",
}
AUDIT_312_COLUMNS = [
    "indicator_id",
    "year",
    "total_live_births",
    "skilled_attended_live_births",
    "included_attendant_categories",
    "calculated_percentage",
    "archive_value",
    "difference_from_archive",
    "matches_archive_precision",
    "source_url",
    "retrieval_method",
    "retrieval_date",
    "source_notes",
]
AUDIT_372_COLUMNS = [
    "indicator_id",
    "year",
    "age_group",
    "live_births",
    "female_population",
    "calculated_rate",
    "source_reported_fertility_rate",
    "archive_value",
    "difference_from_archive",
    "matches_archive_precision",
    "source_url",
    "retrieval_method",
    "retrieval_date",
    "source_notes",
]


def _archive_text(indicator_id: str) -> str:
    member = f"sdg-master/data/indicator_{indicator_id.replace('.', '-')}.csv"
    try:
        return read_nested_zip_member(
            ARCHIVE_PATH, CANONICAL_ZIP_MEMBER, member
        ).decode("utf-8-sig")
    except (ArchiveReadError, UnicodeDecodeError) as error:
        raise RuntimeError(
            f"Could not read canonical archived SDG {indicator_id} data"
        ) from error


def read_archived_3_1_2() -> dict[int, Decimal]:
    values: dict[int, Decimal] = {}
    for row in csv.DictReader(io.StringIO(_archive_text("3.1.2"), newline="")):
        try:
            key = int(row["Year"])
            value = Decimal(row["Value"])
        except (KeyError, ValueError, InvalidOperation) as error:
            raise RuntimeError(f"Invalid archived SDG 3.1.2 row: {row}") from error
        if key in values:
            raise RuntimeError(f"Duplicate archived SDG 3.1.2 year {key}")
        values[key] = value
    return values


def read_archived_3_7_2() -> dict[tuple[int, str], Decimal]:
    values: dict[tuple[int, str], Decimal] = {}
    for row in csv.DictReader(io.StringIO(_archive_text("3.7.2"), newline="")):
        try:
            key = (int(row["Year"]), row["age"].strip())
            value = Decimal(row["Value"])
        except (KeyError, ValueError, InvalidOperation) as error:
            raise RuntimeError(f"Invalid archived SDG 3.7.2 row: {row}") from error
        if key in values:
            raise RuntimeError(f"Duplicate archived SDG 3.7.2 row {key}")
        values[key] = value
    return values


def build_queries() -> list[nvss_natality.NatalityQuery]:
    return [
        nvss_natality.NatalityQuery(
            key="3.1.2",
            dimension=nvss_natality.MEDICAL_ATTENDANT_VARIABLE,
            dimension_values=tuple(nvss_natality.MEDICAL_ATTENDANT_LABELS),
            start_year=2022,
            end_year=2024,
        ),
        nvss_natality.NatalityQuery(
            key="3.7.2",
            dimension=nvss_natality.MATERNAL_AGE_9_VARIABLE,
            dimension_values=tuple(nvss_natality.ADOLESCENT_AGE_LABELS),
            include_fertility_rate=True,
            start_year=2022,
            end_year=2024,
        ),
    ]


def comparison_summary(
    current: Mapping[object, Decimal], archived: Mapping[object, Decimal]
) -> dict[str, object]:
    overlap = sorted(set(current) & set(archived))
    differences = {key: current[key] - archived[key] for key in overlap}
    exact = [
        key
        for key in overlap
        if current[key].quantize(
            Decimal(1).scaleb(archived[key].as_tuple().exponent)
        )
        == archived[key]
    ]
    return {
        "overlapping_rows": len(overlap),
        "matches_at_archive_precision": len(exact),
        "maximum_absolute_difference": (
            max(abs(value) for value in differences.values()) if differences else None
        ),
        "mismatches": [key for key in overlap if key not in set(exact)],
    }


def audit_3_1_2(
    values: list[indicator_3_1_2.SkilledAttendanceYear],
    source: nvss_natality.NvssNatalityResult,
    archived: Mapping[int, Decimal],
) -> list[dict[str, object]]:
    rows = []
    for item in values:
        archive_value = archived.get(item.year)
        rows.append(
            {
                "indicator_id": "3.1.2",
                "year": item.year,
                "total_live_births": item.total_births,
                "skilled_attended_live_births": item.skilled_births,
                "included_attendant_categories": " | ".join(item.included_categories),
                "calculated_percentage": indicator_3_1_2.decimal_text(item.percentage),
                "archive_value": "" if archive_value is None else format(archive_value, "f"),
                "difference_from_archive": (
                    ""
                    if archive_value is None
                    else indicator_3_1_2.decimal_text(item.percentage - archive_value)
                ),
                "matches_archive_precision": (
                    ""
                    if archive_value is None
                    else str(indicator_3_1_2.archive_matches(item.percentage, archive_value)).lower()
                ),
                "source_url": source.births_source_url or source.source_url,
                "retrieval_method": source.retrieval_method,
                "retrieval_date": source.retrieval_date,
                "source_notes": " | ".join(source.source_warnings),
            }
        )
    return rows


def audit_3_7_2(
    values: list[indicator_3_7_2.AdolescentBirthRateYear],
    source: nvss_natality.NvssNatalityResult,
    archived: Mapping[tuple[int, str], Decimal],
) -> list[dict[str, object]]:
    rows = []
    for item in values:
        key = (item.year, item.age_group)
        archive_value = archived.get(key)
        rows.append(
            {
                "indicator_id": "3.7.2",
                "year": item.year,
                "age_group": item.age_group,
                "live_births": item.births,
                "female_population": item.female_population,
                "calculated_rate": indicator_3_7_2.decimal_text(item.rate),
                "source_reported_fertility_rate": (
                    ""
                    if item.source_reported_rate is None
                    else format(item.source_reported_rate, "f")
                ),
                "archive_value": "" if archive_value is None else format(archive_value, "f"),
                "difference_from_archive": (
                    ""
                    if archive_value is None
                    else indicator_3_7_2.decimal_text(item.rate - archive_value)
                ),
                "matches_archive_precision": (
                    ""
                    if archive_value is None
                    else str(indicator_3_7_2.archive_matches(item.rate, archive_value)).lower()
                ),
                "source_url": " | ".join(
                    value
                    for value in (
                        source.births_source_url,
                        source.population_source_url,
                    )
                    if value
                )
                or source.source_url,
                "retrieval_method": source.retrieval_method,
                "retrieval_date": source.retrieval_date,
                "source_notes": " | ".join(source.source_warnings),
            }
        )
    return rows


def run() -> dict[str, object]:
    retrieval_date = current_retrieval_date()
    source = nvss_natality.fetch_natality_batch(
        build_queries(), retrieval_date=retrieval_date
    )
    archived_312 = read_archived_3_1_2()
    archived_372 = read_archived_3_7_2()
    values_312 = indicator_3_1_2.calculate(source.observations["3.1.2"])
    values_372 = indicator_3_7_2.calculate(source.observations["3.7.2"])
    standardized_312 = indicator_3_1_2.build_standardized(
        values_312, source, archived_312
    )
    standardized_372 = indicator_3_7_2.build_standardized(
        values_372, source, archived_372
    )

    write_csv_outputs_atomically(
        [
            (
                STANDARDIZED_PATHS["3.1.2"],
                STANDARDIZED_COLUMNS,
                [observation_to_row(item) for item in standardized_312],
            ),
            (
                STANDARDIZED_PATHS["3.7.2"],
                STANDARDIZED_COLUMNS,
                [observation_to_row(item) for item in standardized_372],
            ),
            (
                AUDIT_PATHS["3.1.2"],
                AUDIT_312_COLUMNS,
                audit_3_1_2(values_312, source, archived_312),
            ),
            (
                AUDIT_PATHS["3.7.2"],
                AUDIT_372_COLUMNS,
                audit_3_7_2(values_372, source, archived_372),
            ),
        ]
    )
    current_312 = {item.year: item.percentage for item in values_312}
    current_372 = {(item.year, item.age_group): item.rate for item in values_372}
    return {
        "source_method": source.retrieval_method,
        "source_url": source.source_url,
        "source_warnings": source.source_warnings,
        "3.1.2": {
            "values": values_312,
            "comparison": comparison_summary(current_312, archived_312),
        },
        "3.7.2": {
            "values": values_372,
            "comparison": comparison_summary(current_372, archived_372),
        },
    }


def main() -> int:
    try:
        result = run()
    except (RetrievalError, RuntimeError, OSError, ValueError) as error:
        print(f"SDG NVSS natality batch failed: {error}", file=sys.stderr)
        return 1
    print(f"Source method: {result['source_method']}")
    print(f"Source URL: {result['source_url']}")
    for warning in result["source_warnings"]:
        print(f"Source warning: {warning}")
    for indicator_id in ("3.1.2", "3.7.2"):
        values = result[indicator_id]["values"]
        comparison = result[indicator_id]["comparison"]
        print(
            f"SDG {indicator_id}: {min(item.year for item in values)}-"
            f"{max(item.year for item in values)}; rows={len(values)}; "
            f"archive overlap={comparison['overlapping_rows']}; "
            f"matches={comparison['matches_at_archive_precision']}; "
            f"maximum difference={comparison['maximum_absolute_difference']}"
        )
    latest_312 = result["3.1.2"]["values"][-1]
    print(
        f"  Latest 3.1.2: {latest_312.year}; skilled={latest_312.skilled_births:,}; "
        f"total={latest_312.total_births:,}; percent={latest_312.percentage:.6f}"
    )
    latest_372_year = max(item.year for item in result["3.7.2"]["values"])
    for item in result["3.7.2"]["values"]:
        if item.year == latest_372_year:
            print(
                f"  Latest 3.7.2 {item.age_group}: births={item.births:,}; "
                f"female population={item.female_population:,}; rate={item.rate:.6f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
