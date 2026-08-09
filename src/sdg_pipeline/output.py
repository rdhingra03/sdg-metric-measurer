"""Shared retrieval-date and failure-safe CSV output helpers."""

from __future__ import annotations

import csv
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence, Tuple


CsvRows = Sequence[Mapping[str, object]]
CsvOutput = Tuple[Path, Sequence[str], CsvRows]


def current_retrieval_date() -> str:
    """Return today's local date in the ISO format used by pipeline outputs."""

    return date.today().isoformat()


def prepare_temporary_csv(
    output_path: Path, columns: Sequence[str], rows: CsvRows
) -> Path:
    """Write and fsync a complete temporary CSV beside its final path."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            writer = csv.DictWriter(output_file, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
            output_file.flush()
            os.fsync(output_file.fileno())
        return temporary_path
    except Exception:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise


def write_csv_atomically(
    output_path: Path, columns: Sequence[str], rows: CsvRows
) -> None:
    """Replace one CSV only after its complete temporary file is durable."""

    temporary_path = prepare_temporary_csv(output_path, columns, rows)
    try:
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def write_csv_outputs_atomically(outputs: Sequence[CsvOutput]) -> None:
    """Prepare all CSVs before replacing any of their prior versions.

    This preserves the existing two-output behavior of the 4.2.2 pipeline. It
    ensures preparation failure changes no final file. As before, separate
    ``os.replace`` calls cannot form a single filesystem transaction.
    """

    temporary_outputs = []
    try:
        for output_path, columns, rows in outputs:
            temporary_path = prepare_temporary_csv(output_path, columns, rows)
            temporary_outputs.append((temporary_path, output_path))

        while temporary_outputs:
            temporary_path, output_path = temporary_outputs[0]
            os.replace(temporary_path, output_path)
            temporary_outputs.pop(0)
    finally:
        for temporary_path, _output_path in temporary_outputs:
            if temporary_path.exists():
                temporary_path.unlink()
