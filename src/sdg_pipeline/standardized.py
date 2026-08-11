"""Shared model and atomic CSV writer for standardized SDG observations.

The source connectors and indicator modules do the substantive work.  This
module has the deliberately small job of making sure every standardized file
uses the same columns, column order, and disaggregation representation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .output import write_csv_atomically


STANDARDIZED_COLUMNS = [
    "indicator_id",
    "indicator_title",
    "year",
    "value",
    "unit",
    "geography",
    "disaggregation",
    "source_organization",
    "source_dataset",
    "source_url",
    "retrieval_method",
    "retrieval_date",
    "methodology_variant",
    "validation_status",
    "data_warning",
]

ARCHIVE_MATCHED = "archive_matched"
ARCHIVE_MISMATCH = "archive_mismatch"
NOT_ARCHIVE_VALIDATED = "not_archive_validated"


@dataclass(frozen=True)
class StandardizedObservation:
    """One observation in the project's common processed-data format."""

    indicator_id: str
    indicator_title: str
    year: int
    value: str | float
    unit: str
    geography: str
    disaggregation: Mapping[str, str]
    source_organization: str
    source_dataset: str
    source_url: str
    retrieval_method: str
    retrieval_date: str
    methodology_variant: str
    validation_status: str
    data_warning: str


def serialize_disaggregation(disaggregation: Mapping[str, str]) -> str:
    """Return stable, compact JSON such as ``{}`` or ``{"sex":"Male"}``."""

    return json.dumps(
        dict(disaggregation),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def observation_to_row(
    observation: StandardizedObservation | Mapping[str, object],
) -> dict[str, object]:
    """Validate and convert an observation to a CSV-ready dictionary.

    Accepting a mapping as well as the dataclass keeps the writer easy to test
    and reuse, while still rejecting missing, extra, or null fields.
    """

    if isinstance(observation, StandardizedObservation):
        values: dict[str, object] = {
            column: getattr(observation, column) for column in STANDARDIZED_COLUMNS
        }
    else:
        values = dict(observation)
        missing = [column for column in STANDARDIZED_COLUMNS if column not in values]
        extra = [column for column in values if column not in STANDARDIZED_COLUMNS]
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing columns: {', '.join(missing)}")
            if extra:
                details.append(f"unexpected columns: {', '.join(extra)}")
            raise ValueError("Invalid standardized observation (" + "; ".join(details) + ")")

    null_columns = [column for column, value in values.items() if value is None]
    if null_columns:
        raise ValueError(
            "Standardized observations cannot contain null values: "
            + ", ".join(null_columns)
        )

    disaggregation = values["disaggregation"]
    if not isinstance(disaggregation, Mapping):
        raise ValueError("disaggregation must be a mapping that can be serialized as JSON")
    values["disaggregation"] = serialize_disaggregation(disaggregation)
    return {column: values[column] for column in STANDARDIZED_COLUMNS}


def write_standardized_csv(
    output_path: Path,
    observations: Sequence[StandardizedObservation | Mapping[str, object]],
) -> None:
    """Validate observations and write them atomically in deterministic order."""

    rows = [observation_to_row(observation) for observation in observations]
    write_csv_atomically(output_path, STANDARDIZED_COLUMNS, rows)
