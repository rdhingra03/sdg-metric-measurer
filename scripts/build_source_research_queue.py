#!/usr/bin/env python3
"""Build the source-research queue from the canonical indicator inventory.

This script does not perform source research. It selects indicators whose
``source_url`` is blank, carries forward useful context, and creates empty
fields for future research decisions. Only ``research_status`` is initialized.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = PROJECT_ROOT / "metadata" / "indicator_inventory.csv"
OUTPUT_PATH = PROJECT_ROOT / "metadata" / "source_research_queue.csv"

CARRIED_FIELDS = [
    "indicator_id",
    "sdg_goal",
    "sdg_target",
    "indicator_title",
    "reporting_status",
    "data_quality",
    "source_organization",
    "source_dataset",
    "computation_method",
    "geographic_coverage",
    "inventory_warnings",
]

RESEARCH_FIELDS = [
    "research_status",
    "us_applicability",
    "proposed_source_organization",
    "proposed_source_dataset",
    "proposed_source_url",
    "source_type",
    "retrieval_method",
    "automation_feasibility",
    "confidence",
    "research_notes",
    "date_verified",
]

OUTPUT_FIELDS = CARRIED_FIELDS + RESEARCH_FIELDS

# This order determines research priority. A populated indicator already has
# figures that need provenance, while a missing-data indicator will usually
# require both source discovery and later data acquisition work.
DATA_QUALITY_ORDER = {
    "populated": 0,
    "single_observation": 1,
    "placeholder": 2,
    "missing": 3,
}


def indicator_sort_parts(indicator_id: str) -> Tuple[Tuple[int, object], ...]:
    """Make dotted indicator IDs sort naturally, including lettered targets."""

    parts: List[Tuple[int, object]] = []
    for part in indicator_id.split("."):
        parts.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(parts)


def queue_sort_key(row: Mapping[str, str]) -> Tuple[object, ...]:
    quality = row["data_quality"]
    if quality not in DATA_QUALITY_ORDER:
        raise ValueError(
            f"Unexpected data_quality {quality!r} for indicator "
            f"{row['indicator_id']!r}"
        )
    try:
        goal = int(row["sdg_goal"])
    except ValueError as error:
        raise ValueError(
            f"Invalid sdg_goal {row['sdg_goal']!r} for "
            f"indicator {row['indicator_id']!r}"
        ) from error
    return DATA_QUALITY_ORDER[quality], goal, indicator_sort_parts(row["indicator_id"])


def read_inventory() -> List[Dict[str, str]]:
    """Read and validate the fields needed from indicator_inventory.csv."""

    with INPUT_PATH.open("r", encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        fieldnames = reader.fieldnames or []
        required_fields = set(CARRIED_FIELDS) | {"source_url"}
        missing_fields = sorted(required_fields - set(fieldnames))
        if missing_fields:
            raise ValueError(
                "Inventory is missing required columns: " + ", ".join(missing_fields)
            )
        return list(reader)


def build_queue(inventory_rows: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    """Select URL-missing indicators and initialize their research fields."""

    queue_rows: List[Dict[str, str]] = []
    seen_ids = set()

    for inventory_row in inventory_rows:
        if inventory_row["source_url"].strip():
            continue

        indicator_id = inventory_row["indicator_id"].strip()
        if indicator_id in seen_ids:
            raise ValueError(f"Duplicate indicator_id in inventory: {indicator_id}")
        seen_ids.add(indicator_id)

        queue_row = {field: inventory_row[field] for field in CARRIED_FIELDS}
        for field in RESEARCH_FIELDS:
            queue_row[field] = ""
        queue_row["research_status"] = "not_started"
        queue_rows.append(queue_row)

    queue_rows.sort(key=queue_sort_key)
    return queue_rows


def write_queue(rows: Sequence[Mapping[str, str]]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def print_validation(rows: Sequence[Mapping[str, str]]) -> None:
    quality_counts = Counter(row["data_quality"] for row in rows)
    goal_counts = Counter(int(row["sdg_goal"]) for row in rows)

    print(f"Wrote {OUTPUT_PATH}")
    print(f"Total queue rows: {len(rows)}")
    print("Counts by data_quality:")
    for quality in DATA_QUALITY_ORDER:
        print(f"  {quality}: {quality_counts[quality]}")
    print("Counts by SDG goal:")
    for goal in range(1, 18):
        print(f"  Goal {goal}: {goal_counts[goal]}")


def main() -> None:
    inventory_rows = read_inventory()
    queue_rows = build_queue(inventory_rows)
    write_queue(queue_rows)
    print_validation(queue_rows)


if __name__ == "__main__":
    main()
