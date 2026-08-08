#!/usr/bin/env python3
"""Build a one-row-per-indicator inventory from the archived Open SDG source.

The legacy source is deliberately read in place.  Nothing is extracted to disk:
the inner ZIP archive is opened from memory after it is read from SDGs.tar.
Only Python's standard library is required.
"""

from __future__ import annotations

import csv
import io
import json
import re
import tarfile
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
CANONICAL_ROOT = "sdg-master"
TRANSLATED_ZIP_MEMBER = "SDGs/sdg-gh-pages.zip"
TRANSLATED_ROOT = "sdg-gh-pages"
OUTPUT_PATH = PROJECT_ROOT / "metadata" / "indicator_inventory.csv"

OUTPUT_COLUMNS = [
    "indicator_id",
    "sdg_goal",
    "sdg_target",
    "indicator_title",
    "reporting_status",
    "data_file_exists",
    "data_quality",
    "observation_count",
    "earliest_year",
    "latest_year",
    "source_organization",
    "source_dataset",
    "source_url",
    "source_url_origin",
    "geographic_coverage",
    "computation_method",
    "inventory_warnings",
]

# Canonical Open SDG filenames use hyphens, for example 1-2-1 and 10-a-1.
INDICATOR_ID_PATTERN = re.compile(r"^(\d+)-(\d+|[a-z])-(\d+|[a-z])$")
TOP_LEVEL_YAML_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):(?:[ \t]*(.*))?$")
URL_PATTERN = re.compile(r"https?://[^\s<>\\\"']+")


def open_canonical_zip() -> zipfile.ZipFile:
    """Open sdg-master.zip from the tar archive without extracting either one."""

    with tarfile.open(ARCHIVE_PATH, mode="r:*") as outer_archive:
        try:
            member = outer_archive.getmember(CANONICAL_ZIP_MEMBER)
        except KeyError as error:
            raise RuntimeError(
                f"Could not find {CANONICAL_ZIP_MEMBER!r} in {ARCHIVE_PATH}"
            ) from error

        archived_file = outer_archive.extractfile(member)
        if archived_file is None:
            raise RuntimeError(f"Could not read {CANONICAL_ZIP_MEMBER!r}")

        # ZipFile needs a seekable input. BytesIO provides that in memory, so no
        # temporary or permanently extracted legacy files are created.
        zip_bytes = io.BytesIO(archived_file.read())

    return zipfile.ZipFile(zip_bytes, mode="r")


def open_nested_zip(member_name: str) -> zipfile.ZipFile:
    """Open any ZIP member of SDGs.tar in memory, without extracting it."""

    with tarfile.open(ARCHIVE_PATH, mode="r:*") as outer_archive:
        try:
            member = outer_archive.getmember(member_name)
        except KeyError as error:
            raise RuntimeError(
                f"Could not find {member_name!r} in {ARCHIVE_PATH}"
            ) from error
        archived_file = outer_archive.extractfile(member)
        if archived_file is None:
            raise RuntimeError(f"Could not read {member_name!r}")
        return zipfile.ZipFile(io.BytesIO(archived_file.read()), mode="r")


def clean_scalar(value: str) -> str:
    """Convert the simple scalar values used by these metadata files to text."""

    value = value.strip()
    if value.lower() in {"", "null", "none", "~"}:
        return ""
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        # YAML permits a backslash at the end of a physical line to join a
        # long double-quoted value. Some legacy files also begin the next line
        # with ``\ ``. Resolve that notation before decoding simple escapes.
        value = value[1:-1]
        value = re.sub(r"\\\n[ \t]*\\?[ \t]*", " ", value)
        value = (
            value.replace(r"\t", " ")
            .replace(r"\n", " ")
            .replace(r"\r", " ")
            .replace(r'\"', '"')
            .replace(r"\/", "/")
            .replace(r"\ ", " ")
        )
        value = value.replace(r"\\", "\\").strip()

    # Treat textual null values consistently even when they were quoted or
    # followed by escaped tabs in the source.
    return "" if value.strip().lower() in {"", "null", "none", "~"} else value


def parse_top_level_yaml(text: str) -> Dict[str, str]:
    """Read top-level YAML fields needed for the inventory.

    The archive does not include a Python dependency manifest and PyYAML is not
    part of Python itself. The source metadata uses straightforward top-level
    keys, quoted values, and indented folded text, so a small purpose-built
    reader keeps this deliverable dependency-free. Nested display settings are
    intentionally ignored because none are inventory columns.
    """

    lines = text.lstrip("\ufeff").splitlines()
    result: Dict[str, str] = {}
    index = 0

    while index < len(lines):
        match = TOP_LEVEL_YAML_KEY.match(lines[index])
        if not match:
            index += 1
            continue

        key = match.group(1)
        first_value = (match.group(2) or "").strip()
        continuation: List[str] = []
        index += 1

        while index < len(lines) and not TOP_LEVEL_YAML_KEY.match(lines[index]):
            line = lines[index]
            # Only indented lines belong to the current YAML field. Unindented
            # document markers and comments are not part of its value.
            if line.startswith((" ", "\t")) or not line.strip():
                continuation.append(line.strip())
            index += 1

        if first_value.startswith((">", "|")):
            # Fold wrapped prose into readable single-line CSV cells. Blank
            # lines remain separators until whitespace is normalized below.
            value = " ".join(continuation)
        elif first_value.startswith('"') and continuation:
            # Preserve physical line boundaries long enough for clean_scalar
            # to recognize YAML's backslash line-continuation syntax.
            value = "\n".join([first_value, *continuation])
        elif continuation and first_value not in {"[]", "{}"}:
            value = " ".join([first_value, *continuation])
        else:
            value = first_value

        result[key] = re.sub(r"\s+", " ", clean_scalar(value)).strip()

    return result


def indicator_id_from_path(path: str, folder: str) -> str | None:
    """Return a canonical hyphenated ID from a source filename, if it has one."""

    name = Path(path).stem
    if folder == "data" and name.startswith("indicator_"):
        name = name[len("indicator_") :]
    return name if INDICATOR_ID_PATTERN.fullmatch(name) else None


def collect_paths(
    archive: zipfile.ZipFile,
) -> Tuple[Dict[str, List[str]], Dict[str, str], Dict[str, str]]:
    """Index metadata, configuration, and data paths by unique indicator ID."""

    metadata_paths: Dict[str, List[str]] = {}
    config_paths: Dict[str, str] = {}
    data_paths: Dict[str, str] = {}

    for path in archive.namelist():
        for folder in ("meta", "indicator-config", "data"):
            prefix = f"{CANONICAL_ROOT}/{folder}/"
            if not path.startswith(prefix) or path.endswith("/"):
                continue

            indicator_id = indicator_id_from_path(path, folder)
            if indicator_id is None:
                continue

            if folder == "meta":
                metadata_paths.setdefault(indicator_id, []).append(path)
            elif folder == "indicator-config":
                config_paths[indicator_id] = path
            else:
                data_paths[indicator_id] = path

    return metadata_paths, config_paths, data_paths


def read_yaml_fields(archive: zipfile.ZipFile, path: str) -> Dict[str, str]:
    text = archive.read(path).decode("utf-8-sig", errors="replace")
    return parse_top_level_yaml(text)


def merged_metadata(
    archive: zipfile.ZipFile, paths: Sequence[str]
) -> Dict[str, str]:
    """Merge duplicate metadata files without replacing useful non-empty values."""

    merged: Dict[str, str] = {}
    # Markdown files are the native Open SDG records. A same-ID YAML file can
    # fill fields that are absent from the Markdown record.
    ordered_paths = sorted(paths, key=lambda path: (not path.endswith(".md"), path))
    for path in ordered_paths:
        for key, value in read_yaml_fields(archive, path).items():
            if value and not merged.get(key):
                merged[key] = value
    return merged


def first_useful(fields: Iterable[Mapping[str, str]], keys: Sequence[str]) -> str:
    """Return the first populated value found using the stated precedence."""

    for key in keys:
        for field_set in fields:
            value = field_set.get(key, "").strip()
            if value:
                return value
    return ""


def load_english_title_translations() -> Dict[str, str]:
    """Load already-resolved English titles from the archived published build.

    sdg-master references the external ``sdg-translations`` package but does not
    bundle its global title catalog. The matching sdg-gh-pages snapshot in the
    same immutable tar archive contains the resolved English titles. It is used
    only as a translation lookup, never to add indicators or supply their data,
    status, or descriptive metadata.
    """

    titles: Dict[str, str] = {}
    with open_nested_zip(TRANSLATED_ZIP_MEMBER) as translated_archive:
        prefix = f"{TRANSLATED_ROOT}/en/meta/"
        for path in translated_archive.namelist():
            if not path.startswith(prefix) or not path.endswith(".json"):
                continue
            indicator_id = Path(path).stem
            if not INDICATOR_ID_PATTERN.fullmatch(indicator_id):
                continue
            try:
                metadata = json.loads(translated_archive.read(path))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            title = str(
                metadata.get("indicator_name") or metadata.get("graph_title") or ""
            ).strip()
            if title and not title.startswith("global_indicators."):
                titles[indicator_id] = title
    return titles


def choose_title(
    metadata: Mapping[str, str],
    config: Mapping[str, str],
    translated_titles: Mapping[str, str],
    indicator_id: str,
) -> str:
    candidates = [
        metadata.get("title", ""),
        metadata.get("SDG_INDICATOR", ""),
        metadata.get("actual_indicator_available_description", ""),
        config.get("indicator_available", ""),
        config.get("graph_title", ""),
        metadata.get("graph_title", ""),
        config.get("indicator_name", ""),
        metadata.get("indicator_name", ""),
    ]
    # Prefer readable text over translation tokens such as
    # "global_indicators.1-2-1-title".
    for candidate in candidates:
        if candidate and not candidate.startswith("global_indicators."):
            return candidate
    return translated_titles.get(indicator_id, "")


def urls_from_keys(metadata: Mapping[str, str], keys: Iterable[str]) -> str:
    """Collect unique web addresses from a specified, ordered set of fields."""

    urls: List[str] = []
    for key in keys:
        for url in URL_PATTERN.findall(metadata[key]):
            url = url.rstrip(".,;)")
            if url not in urls:
                urls.append(url)
    return " | ".join(urls)


def collect_source_urls(metadata: Mapping[str, str]) -> Tuple[str, str]:
    """Select source URLs using explicit fields first, then narrow fallbacks."""

    explicit_keys = [
        key
        for key in sorted(metadata)
        if key.startswith("source_url_") and not key.startswith("source_url_text_")
    ]
    explicit_urls = urls_from_keys(metadata, explicit_keys)
    if explicit_urls:
        return explicit_urls, "explicit_source_url"

    # Search only fields whose names clearly describe a source. Stop at the
    # first category with URLs so the origin remains simple and unambiguous.
    fallback_categories = [
        (
            "source_dataset_field",
            [
                key
                for key in sorted(metadata)
                if key == "SOURCE_TYPE"
                or key == "DATA_SOURCE"
                or key.startswith("source_agency_survey_dataset_")
                or key.startswith("source_title_")
            ],
        ),
        (
            "source_notes",
            [key for key in sorted(metadata) if key.startswith("source_notes_")],
        ),
        (
            "reference_field",
            ["international_and_national_references"]
            if "international_and_national_references" in metadata
            else [],
        ),
    ]
    for origin, keys in fallback_categories:
        urls = urls_from_keys(metadata, keys)
        if urls:
            return urls, origin
    return "", "missing"


def summarize_data_file(
    archive: zipfile.ZipFile, path: str
) -> Tuple[int, str, str, str]:
    """Count observations, find their year range, and classify data quality."""

    observations = 0
    years: List[int] = []

    raw_text = archive.read(path).decode("utf-8-sig", errors="replace")
    normalized_text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    is_placeholder = normalized_text == "Year,Value\n2015,0"

    try:
        reader = csv.DictReader(io.StringIO(raw_text, newline=""))
        fieldnames = reader.fieldnames or []
        value_column = next(
            (name for name in fieldnames if name.strip().lower() == "value"), None
        )
        year_column = next(
            (name for name in fieldnames if name.strip().lower() == "year"), None
        )

        if value_column is None:
            raise RuntimeError(f"Canonical data file has no Value column: {path}")

        for row in reader:
            value = (row.get(value_column) or "").strip()
            if not value:
                continue
            observations += 1
            year = (row.get(year_column) or "").strip() if year_column else ""
            if re.fullmatch(r"\d{4}", year):
                years.append(int(year))
    except (csv.Error, UnicodeError):
        raise RuntimeError(f"Could not safely parse canonical data file: {path}")

    if is_placeholder:
        data_quality = "placeholder"
    elif observations == 1:
        data_quality = "single_observation"
    elif observations > 1:
        data_quality = "populated"
    else:
        # The requested classification has no separate empty-file category.
        # Fail visibly if a future archive introduces one rather than silently
        # assigning an inaccurate label.
        raise RuntimeError(f"Canonical data file contains no observations: {path}")

    return (
        observations,
        str(min(years)) if years else "",
        str(max(years)) if years else "",
        data_quality,
    )


def indicator_sort_key(indicator_id: str) -> Tuple[int, Tuple[Tuple[int, object], ...]]:
    """Sort 2-1-1 before 10-1-1 while also supporting lettered targets."""

    parts = indicator_id.split("-")
    sortable_parts: List[Tuple[int, object]] = []
    for part in parts[1:]:
        sortable_parts.append((0, int(part)) if part.isdigit() else (1, part))
    return int(parts[0]), tuple(sortable_parts)


def discover_alternate_files(
    archive: zipfile.ZipFile,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Find noncanonical filenames that normalize to a canonical indicator ID."""

    alternate_data: Dict[str, List[str]] = {}
    alternate_metadata: Dict[str, List[str]] = {}
    for path in archive.namelist():
        if path.endswith("/"):
            continue
        folder = ""
        stem = Path(path).stem
        if path.startswith(f"{CANONICAL_ROOT}/data/") and stem.startswith("indicator_"):
            folder = "data"
            stem = stem[len("indicator_") :]
        elif path.startswith(f"{CANONICAL_ROOT}/meta/"):
            folder = "meta"
        else:
            continue

        if INDICATOR_ID_PATTERN.fullmatch(stem):
            continue
        normalized_id = stem.replace("_", "-").replace(".", "-")
        if not INDICATOR_ID_PATTERN.fullmatch(normalized_id):
            continue
        short_path = path[len(CANONICAL_ROOT) + 1 :]
        destination = alternate_data if folder == "data" else alternate_metadata
        destination.setdefault(normalized_id, []).append(short_path)
    return alternate_data, alternate_metadata


def build_inventory(
    archive: zipfile.ZipFile, translated_titles: Mapping[str, str]
) -> List[Dict[str, object]]:
    metadata_paths, config_paths, data_paths = collect_paths(archive)
    alternate_data, alternate_metadata = discover_alternate_files(archive)
    indicator_ids = set(metadata_paths) | set(config_paths) | set(data_paths)
    rows: List[Dict[str, object]] = []

    for source_id in sorted(indicator_ids, key=indicator_sort_key):
        metadata = merged_metadata(archive, metadata_paths.get(source_id, []))
        config = (
            read_yaml_fields(archive, config_paths[source_id])
            if source_id in config_paths
            else {}
        )

        data_path = data_paths.get(source_id)
        observations, earliest_year, latest_year, data_quality = (
            summarize_data_file(archive, data_path)
            if data_path
            else (0, "", "", "missing")
        )

        reporting_status = first_useful(
            (config, metadata), ("reporting_status",)
        )
        source_url, source_url_origin = collect_source_urls(metadata)
        warnings: List[str] = []
        config_status = config.get("reporting_status", "")
        metadata_status = metadata.get("reporting_status", "")
        if config_status and metadata_status and config_status != metadata_status:
            warnings.append(
                "reporting_status_conflict: "
                f"indicator-config={config_status}, metadata={metadata_status}"
            )
        if source_id in alternate_data:
            warnings.append(
                "nonstandard_alternate_data_file: "
                + ", ".join(sorted(alternate_data[source_id]))
            )
        if source_id in alternate_metadata:
            warnings.append(
                "alternate_metadata_files: "
                + ", ".join(sorted(alternate_metadata[source_id]))
            )

        goal, target, indicator = source_id.split("-")
        row: Dict[str, object] = {
            "indicator_id": ".".join((goal, target, indicator)),
            "sdg_goal": goal,
            "sdg_target": ".".join((goal, target)),
            "indicator_title": choose_title(
                metadata, config, translated_titles, source_id
            ),
            "reporting_status": reporting_status,
            "data_file_exists": "true" if data_path else "false",
            "data_quality": data_quality,
            "observation_count": observations,
            "earliest_year": earliest_year,
            "latest_year": latest_year,
            "source_organization": first_useful(
                (metadata,),
                ("CONTACT_ORGANISATION", "source_organisation_1", "DATA_SOURCE"),
            ),
            "source_dataset": first_useful(
                (metadata,),
                ("source_agency_survey_dataset_1", "source_title_1", "SOURCE_TYPE"),
            ),
            "source_url": source_url,
            "source_url_origin": source_url_origin,
            "geographic_coverage": first_useful(
                (metadata, config),
                ("national_geographical_coverage", "disaggregation_geography"),
            ),
            "computation_method": first_useful(
                (metadata,),
                ("method_of_computation", "us_method_of_computation", "DATA_COMP"),
            ),
            "inventory_warnings": " | ".join(warnings),
        }
        rows.append(row)

    return rows


def write_inventory(rows: Sequence[Mapping[str, object]]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def print_validation(rows: Sequence[Mapping[str, object]]) -> None:
    indicator_ids = [str(row["indicator_id"]) for row in rows]
    results = {
        "unique indicators": len(set(indicator_ids)),
        "populated": sum(row["data_quality"] == "populated" for row in rows),
        "single observation": sum(
            row["data_quality"] == "single_observation" for row in rows
        ),
        "placeholder": sum(row["data_quality"] == "placeholder" for row in rows),
        "missing": sum(row["data_quality"] == "missing" for row in rows),
        "marked complete": sum(row["reporting_status"] == "complete" for row in rows),
        "not started": sum(row["reporting_status"] == "notstarted" for row in rows),
        "with data files": sum(row["data_file_exists"] == "true" for row in rows),
        "with source URLs": sum(bool(row["source_url"]) for row in rows),
    }

    print(f"Wrote {OUTPUT_PATH}")
    print("Validation results:")
    for label, value in results.items():
        print(f"  {label}: {value}")


def main() -> None:
    translated_titles = load_english_title_translations()
    with open_canonical_zip() as archive:
        rows = build_inventory(archive, translated_titles)
    write_inventory(rows)
    print_validation(rows)


if __name__ == "__main__":
    main()
