#!/usr/bin/env python3
"""Fetch and calculate the legacy U.S. implementation of SDG 4.2.2.

The measure is the weighted percentage of 5-year-olds enrolled in organized
learning (prekindergarten, kindergarten, or first grade or higher) in the
October Current Population Survey School Enrollment Supplement.

The script prefers the Census Microdata API when CENSUS_API_KEY is configured.
It otherwise falls back to official compressed public-use microdata files.
Downloaded files are parsed in memory and are not permanently extracted.

Important historical details:
* Age is age at last birthday at the October CPS interview.
* 2000--2005 use the basic final person weight PWSSWGT.
* 2006 onward use the School Enrollment Supplement weight PWSUPWGT.
* The 2020 estimate may be affected by pandemic-era response and enrollment
  classification issues documented by the Census Bureau.

Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Dict, Iterator, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_PATH = PROJECT_ROOT / "source_materials" / "SDGs.tar"
CANONICAL_ZIP_MEMBER = "SDGs/sdg-master.zip"
CANONICAL_DATA_PATH = "sdg-master/data/indicator_4-2-2.csv"

NATIONAL_OUTPUT_PATH = PROJECT_ROOT / "data_processed" / "sdg_4_2_2.csv"
SEX_OUTPUT_PATH = PROJECT_ROOT / "data_processed" / "sdg_4_2_2_by_sex.csv"

DEFAULT_START_YEAR = 2018
DEFAULT_END_YEAR = 2024
API_FIRST_YEAR = 2000
API_LAST_YEAR = 2024
HTTP_TIMEOUT_SECONDS = 180
USER_AGENT = "sdg-metric-measurer/1.0 (official Census public data client)"

# CPS person weights in these files have four implied decimal places. Keeping
# the raw integer weights makes the percentage calculation exact; output counts
# are divided by this scale to show estimated people rather than storage units.
WEIGHT_SCALE = 10_000
ORGANIZED_LEARNING_GRADES = range(1, 17)

NATIONAL_COLUMNS = [
    "year",
    "weighted_numerator",
    "weighted_denominator",
    "unweighted_numerator",
    "unweighted_denominator",
    "weight_variable",
    "calculated_value",
    "source_url",
    "retrieval_method",
    "retrieval_date",
]
SEX_COLUMNS = ["year", "sex", *NATIONAL_COLUMNS[1:]]


class RetrievalError(RuntimeError):
    """Raised when an official source cannot provide valid, complete data."""


@dataclass(frozen=True)
class FieldPosition:
    """One inclusive, one-based field location from a CPS record layout."""

    start: int
    end: int

    def read(self, record: bytes) -> str:
        """Read and trim this field from one fixed-width record."""

        return record[self.start - 1 : self.end].decode(
            "ascii", errors="strict"
        ).strip()


@dataclass(frozen=True)
class FixedWidthLayout:
    """Published variable positions shared by one or more CPS file years."""

    name: str
    minimum_record_length: int
    prtage: FieldPosition
    pesex: FieldPosition
    pesch35: FieldPosition
    pechgrde: FieldPosition
    weight: FieldPosition


@dataclass(frozen=True)
class DownloadConfig:
    """Official fallback file and parsing instructions for one survey year."""

    url: str
    file_format: str
    layout: FixedWidthLayout
    archive_member: str | None = None


@dataclass(frozen=True)
class PersonRecord:
    """The five CPS fields needed for this indicator."""

    age: int
    enrollment: int
    grade: int
    sex: int
    raw_weight: int


@dataclass(frozen=True)
class GroupResult:
    """Weighted and unweighted results for one national or sex group."""

    raw_weighted_numerator: int
    raw_weighted_denominator: int
    unweighted_numerator: int
    unweighted_denominator: int
    calculated_fraction: Fraction


@dataclass(frozen=True)
class YearResult:
    """Calculated results and source provenance for one survey year."""

    year: int
    weight_variable: str
    source_url: str
    retrieval_method: str
    national: GroupResult
    male: GroupResult
    female: GroupResult


@dataclass(frozen=True)
class ArchivedValue:
    """One archived value plus its displayed decimal precision."""

    value: Decimal
    decimal_places: int


# The October 2018--2024 technical documentation gives the same locations for
# all variables needed here. A named layout object is used rather than assuming
# that every historical CPS year has the same fixed-width structure. Adding an
# older fallback year requires checking its own technical documentation and
# either reusing a verified layout or defining another one.
LAYOUT_2018_2024 = FixedWidthLayout(
    name="CPS October School Enrollment 2018-2024",
    minimum_record_length=1090,
    prtage=FieldPosition(122, 123),
    pesex=FieldPosition(129, 130),
    pesch35=FieldPosition(1027, 1028),
    pechgrde=FieldPosition(1033, 1034),
    weight=FieldPosition(1081, 1090),
)


def census_download_url(year: int) -> str:
    """Return the official Census ZIP URL for a recent October CPS file."""

    short_year = str(year)[-2:]
    return (
        "https://www2.census.gov/programs-surveys/cps/datasets/"
        f"{year}/supp/oct{short_year}pub.zip"
    )


# Each supported fallback year is listed explicitly. This makes file names,
# formats, and layout decisions easy to audit and prevents a future layout
# change from silently being parsed with an old configuration.
DOWNLOAD_CONFIGS: Dict[int, DownloadConfig] = {
    year: DownloadConfig(
        url=census_download_url(year),
        file_format="zip",
        layout=LAYOUT_2018_2024,
        archive_member=f"oct{str(year)[-2:]}pub.dat",
    )
    for year in range(2018, 2025)
}
# The 2019 ZIP alone stores the data file inside a nested Census production
# directory. Keeping this exception in the year configuration avoids guessing
# or silently selecting an unexpected ZIP member.
DOWNLOAD_CONFIGS[2019] = DownloadConfig(
    url=census_download_url(2019),
    file_format="zip",
    layout=LAYOUT_2018_2024,
    archive_member="cpspb/supp/data/oct19/oct19pub.dat",
)


def weight_variable_for_year(year: int) -> str:
    """Return the historically correct person weight for a survey year."""

    return "PWSSWGT" if year <= 2005 else "PWSUPWGT"


def api_dataset_url(year: int) -> str:
    """Return the public landing page for one Census API dataset."""

    return f"https://api.census.gov/data/{year}/cps/school/oct.html"


def request_bytes(
    request: urllib.request.Request, display_url: str
) -> tuple[bytes, str]:
    """Read one HTTP response without exposing an API key in errors."""

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read(), response.headers.get_content_type()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise RetrievalError(f"Request failed for {display_url}: {reason}") from error


def parse_integer(value: object, variable: str, year: int) -> int:
    """Parse a required CPS integer and produce a useful error if malformed."""

    text = str(value).strip()
    try:
        return int(text)
    except ValueError as error:
        raise RetrievalError(
            f"Invalid {variable} value in {year} CPS data: {text!r}"
        ) from error


def person_from_mapping(
    row: Mapping[str, object], year: int, weight_variable: str
) -> PersonRecord:
    """Build a validated person record from API-style named fields."""

    return PersonRecord(
        age=parse_integer(row.get("PRTAGE", ""), "PRTAGE", year),
        enrollment=parse_integer(row.get("PESCH35", ""), "PESCH35", year),
        grade=parse_integer(row.get("PECHGRDE", ""), "PECHGRDE", year),
        sex=parse_integer(row.get("PESEX", ""), "PESEX", year),
        raw_weight=parse_integer(
            row.get(weight_variable, ""), weight_variable, year
        ),
    )


def fetch_from_api(year: int, api_key: str) -> list[PersonRecord]:
    """Fetch age-5 person records from the official Census Microdata API."""

    if not (API_FIRST_YEAR <= year <= API_LAST_YEAR):
        raise RetrievalError(f"The configured Census API does not support {year}")

    weight_variable = weight_variable_for_year(year)
    variables = ["PRTAGE", "PESCH35", "PECHGRDE", "PESEX", weight_variable]
    base_url = f"https://api.census.gov/data/{year}/cps/school/oct"
    query = urllib.parse.urlencode(
        {
            "get": ",".join(variables),
            "PRTAGE": "5",
            "key": api_key,
        }
    )
    request = urllib.request.Request(
        f"{base_url}?{query}",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    body, content_type = request_bytes(request, api_dataset_url(year))

    if body.lstrip().startswith(b"<") or content_type not in {
        "application/json",
        "text/json",
        "text/plain",
    }:
        preview = body.lstrip()[:80].decode("utf-8", errors="replace")
        raise RetrievalError(
            f"Census API returned a non-JSON response for {year} "
            f"({content_type!r}; starts with {preview!r})"
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RetrievalError(f"Census API returned invalid JSON for {year}") from error

    if not isinstance(payload, list) or len(payload) < 2:
        raise RetrievalError(f"Census API returned no person records for {year}")
    header = payload[0]
    if not isinstance(header, list) or any(
        variable not in header for variable in variables
    ):
        raise RetrievalError(f"Census API response omitted required fields for {year}")

    records: list[PersonRecord] = []
    for values in payload[1:]:
        if not isinstance(values, list) or len(values) != len(header):
            raise RetrievalError(f"Census API returned a malformed row for {year}")
        records.append(
            person_from_mapping(dict(zip(header, values)), year, weight_variable)
        )
    return records


def open_downloaded_records(
    body: bytes, config: DownloadConfig, year: int
) -> Iterator[bytes]:
    """Yield raw records from a validated ZIP or gzip response in memory."""

    if config.file_format == "zip":
        if not body.startswith(b"PK"):
            raise RetrievalError(f"Census download for {year} is not a ZIP file")
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                member = config.archive_member
                if member is None or member not in archive.namelist():
                    raise RetrievalError(
                        f"Census ZIP for {year} does not contain {member!r}"
                    )
                with archive.open(member) as stream:
                    yield from stream
        except zipfile.BadZipFile as error:
            raise RetrievalError(f"Census ZIP for {year} is corrupt") from error
        return

    if config.file_format == "gzip":
        if not body.startswith(b"\x1f\x8b"):
            raise RetrievalError(f"Census download for {year} is not gzip data")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                yield from stream
        except OSError as error:
            raise RetrievalError(f"Census gzip file for {year} is corrupt") from error
        return

    raise RetrievalError(
        f"Unsupported configured file format for {year}: {config.file_format}"
    )


def parse_fixed_width_record(
    raw_line: bytes, layout: FixedWidthLayout, year: int, weight_variable: str
) -> PersonRecord | None:
    """Parse one person using the year's verified fixed-width layout."""

    record = raw_line.rstrip(b"\r\n")
    if not record:
        return None
    if len(record) < layout.minimum_record_length:
        raise RetrievalError(
            f"Short fixed-width record in {year}: expected at least "
            f"{layout.minimum_record_length} bytes, found {len(record)}"
        )

    age = parse_integer(layout.prtage.read(record), "PRTAGE", year)
    if age != 5:
        return None
    return PersonRecord(
        age=age,
        enrollment=parse_integer(layout.pesch35.read(record), "PESCH35", year),
        grade=parse_integer(layout.pechgrde.read(record), "PECHGRDE", year),
        sex=parse_integer(layout.pesex.read(record), "PESEX", year),
        raw_weight=parse_integer(layout.weight.read(record), weight_variable, year),
    )


def fetch_from_download(year: int) -> tuple[list[PersonRecord], str]:
    """Download and parse one configured official public-use microdata file."""

    config = DOWNLOAD_CONFIGS.get(year)
    if config is None:
        raise RetrievalError(
            f"No verified downloadable-file layout is configured for {year}. "
            "Use a supported API year with CENSUS_API_KEY or add a layout "
            "from that year's official technical documentation."
        )

    request = urllib.request.Request(config.url, headers={"User-Agent": USER_AGENT})
    body, _content_type = request_bytes(request, config.url)
    weight_variable = weight_variable_for_year(year)
    records = []
    for raw_line in open_downloaded_records(body, config, year):
        record = parse_fixed_width_record(
            raw_line, config.layout, year, weight_variable
        )
        if record is not None:
            records.append(record)
    if not records:
        raise RetrievalError(f"No 5-year-old records were found in the {year} file")
    return records, config.url


def retrieve_year(
    year: int, api_key: str | None
) -> tuple[list[PersonRecord], str, str]:
    """Prefer the API when configured, then use the official download fallback."""

    if api_key and API_FIRST_YEAR <= year <= API_LAST_YEAR:
        try:
            return fetch_from_api(year, api_key), "api", api_dataset_url(year)
        except RetrievalError as api_error:
            print(
                f"{year}: Census API unavailable or invalid: {api_error}\n"
                "  Trying the official downloadable microdata fallback...",
                file=sys.stderr,
            )
    elif not api_key:
        print(
            f"{year}: CENSUS_API_KEY is not configured; using the official "
            "downloadable microdata fallback.",
            file=sys.stderr,
        )

    records, source_url = fetch_from_download(year)
    return records, "download", source_url


def calculate_group(
    records: Sequence[PersonRecord], label: str, year: int
) -> GroupResult:
    """Calculate one weighted group, requiring a nonempty positive denominator."""

    denominator_records = [record for record in records if record.raw_weight > 0]
    numerator_records = [
        record for record in denominator_records if record.enrollment == 1
    ]
    raw_denominator = sum(record.raw_weight for record in denominator_records)
    raw_numerator = sum(record.raw_weight for record in numerator_records)
    if not denominator_records or raw_denominator <= 0:
        raise RuntimeError(f"{year} {label} has no valid positive-weight records")
    return GroupResult(
        raw_weighted_numerator=raw_numerator,
        raw_weighted_denominator=raw_denominator,
        unweighted_numerator=len(numerator_records),
        unweighted_denominator=len(denominator_records),
        calculated_fraction=Fraction(100 * raw_numerator, raw_denominator),
    )


def calculate_year(
    year: int,
    records: Sequence[PersonRecord],
    retrieval_method: str,
    source_url: str,
) -> YearResult:
    """Validate person codes and calculate national, male, and female values."""

    if any(record.age != 5 for record in records):
        raise RuntimeError(f"{year} retrieval included a person who is not age 5")

    invalid_enrollment = sorted(
        {record.enrollment for record in records if record.enrollment not in {1, 2}}
    )
    if invalid_enrollment:
        raise RuntimeError(
            f"{year} has invalid PESCH35 codes for age-5 records: "
            + ", ".join(map(str, invalid_enrollment))
        )

    invalid_grades = sorted(
        {
            record.grade
            for record in records
            if record.enrollment == 1
            and record.grade not in ORGANIZED_LEARNING_GRADES
        }
    )
    if invalid_grades:
        raise RuntimeError(
            f"{year} has enrolled age-5 records outside the organized-learning "
            "grade range: "
            + ", ".join(map(str, invalid_grades))
        )

    invalid_sex = sorted({record.sex for record in records if record.sex not in {1, 2}})
    if invalid_sex:
        raise RuntimeError(
            f"{year} has invalid PESEX codes: " + ", ".join(map(str, invalid_sex))
        )

    male_records = [record for record in records if record.sex == 1]
    female_records = [record for record in records if record.sex == 2]
    national = calculate_group(records, "national", year)
    male = calculate_group(male_records, "male", year)
    female = calculate_group(female_records, "female", year)

    if (
        male.raw_weighted_denominator + female.raw_weighted_denominator
        != national.raw_weighted_denominator
        or male.raw_weighted_numerator + female.raw_weighted_numerator
        != national.raw_weighted_numerator
    ):
        raise RuntimeError(f"{year} male and female totals do not reconcile nationally")

    return YearResult(
        year=year,
        weight_variable=weight_variable_for_year(year),
        source_url=source_url,
        retrieval_method=retrieval_method,
        national=national,
        male=male,
        female=female,
    )


def fraction_to_decimal(value: Fraction) -> Decimal:
    """Convert an exact fraction to a high-precision Decimal for output."""

    with localcontext() as context:
        context.prec = 50
        return Decimal(value.numerator) / Decimal(value.denominator)


def decimal_text(value: Decimal) -> str:
    """Write ordinary decimal notation, trimming only insignificant zeros."""

    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def weighted_population_text(raw_weight_sum: int) -> str:
    """Write a raw four-implied-decimal CPS weight sum as estimated people."""

    value = Decimal(raw_weight_sum) / Decimal(WEIGHT_SCALE)
    return format(value, ".4f")


def group_output_fields(group: GroupResult) -> Dict[str, object]:
    """Return the shared audit columns for one calculated group."""

    return {
        "weighted_numerator": weighted_population_text(
            group.raw_weighted_numerator
        ),
        "weighted_denominator": weighted_population_text(
            group.raw_weighted_denominator
        ),
        "unweighted_numerator": group.unweighted_numerator,
        "unweighted_denominator": group.unweighted_denominator,
        "calculated_value": decimal_text(
            fraction_to_decimal(group.calculated_fraction)
        ),
    }


def build_output_rows(
    results: Sequence[YearResult], retrieval_date: str
) -> tuple[list[Dict[str, object]], list[Dict[str, object]]]:
    """Build national and sex-disaggregated output rows."""

    national_rows: list[Dict[str, object]] = []
    sex_rows: list[Dict[str, object]] = []
    for result in results:
        shared = {
            "year": result.year,
            "weight_variable": result.weight_variable,
            "source_url": result.source_url,
            "retrieval_method": result.retrieval_method,
            "retrieval_date": retrieval_date,
        }
        national_rows.append({**shared, **group_output_fields(result.national)})
        for sex, group in (("Male", result.male), ("Female", result.female)):
            sex_rows.append(
                {**shared, "sex": sex, **group_output_fields(group)}
            )
    return national_rows, sex_rows


def read_archived_values() -> Dict[tuple[int, str], ArchivedValue]:
    """Read national and sex values directly from sdg-master.zip in SDGs.tar."""

    try:
        with tarfile.open(ARCHIVE_PATH, mode="r:*") as outer_archive:
            member = outer_archive.getmember(CANONICAL_ZIP_MEMBER)
            archived_zip = outer_archive.extractfile(member)
            if archived_zip is None:
                raise RuntimeError(f"Could not read {CANONICAL_ZIP_MEMBER}")
            zip_bytes = io.BytesIO(archived_zip.read())

        with zipfile.ZipFile(zip_bytes) as canonical_archive:
            csv_text = canonical_archive.read(CANONICAL_DATA_PATH).decode("utf-8-sig")
    except (KeyError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise RuntimeError(
            "Could not read the archived canonical SDG 4.2.2 CSV"
        ) from error

    archived: Dict[tuple[int, str], ArchivedValue] = {}
    for row in csv.DictReader(io.StringIO(csv_text, newline="")):
        if (row.get("Income") or "").strip():
            continue
        sex = (row.get("Sex") or "").strip() or "National"
        if sex not in {"National", "Male", "Female"}:
            continue
        value_text = (row.get("Value") or "").strip()
        try:
            year = int(row["Year"])
            value = Decimal(value_text)
        except (KeyError, ValueError, ArithmeticError) as error:
            raise RuntimeError(f"Invalid archived SDG row: {row}") from error
        decimal_places = len(value_text.partition(".")[2]) if "." in value_text else 0
        key = (year, sex)
        if key in archived:
            raise RuntimeError(f"Duplicate archived SDG row: {year} {sex}")
        archived[key] = ArchivedValue(value, decimal_places)
    return archived


def compare_at_archived_precision(
    calculated: Fraction, archived: ArchivedValue
) -> Decimal:
    """Return absolute difference after matching the archive's stored precision."""

    quantizer = Decimal(1).scaleb(-archived.decimal_places)
    rounded = fraction_to_decimal(calculated).quantize(
        quantizer, rounding=ROUND_HALF_UP
    )
    return abs(rounded - archived.value)


def validate_results(
    results: Sequence[YearResult], archived: Mapping[tuple[int, str], ArchivedValue]
) -> Dict[str, object]:
    """Validate every available national and sex result against the archive."""

    national_differences: Dict[int, Decimal] = {}
    sex_differences: Dict[tuple[int, str], Decimal] = {}
    for result in results:
        national_key = (result.year, "National")
        if national_key in archived:
            national_differences[result.year] = compare_at_archived_precision(
                result.national.calculated_fraction, archived[national_key]
            )
        for sex, group in (("Male", result.male), ("Female", result.female)):
            key = (result.year, sex)
            if key in archived:
                sex_differences[key] = compare_at_archived_precision(
                    group.calculated_fraction, archived[key]
                )

    if not national_differences:
        raise RuntimeError("No overlapping national years exist for archive validation")

    national_mismatches = [
        year for year, difference in national_differences.items() if difference != 0
    ]
    sex_mismatches = [
        key for key, difference in sex_differences.items() if difference != 0
    ]
    return {
        "national_overlaps": len(national_differences),
        "national_exact_matches": len(national_differences) - len(national_mismatches),
        "national_maximum_difference": max(national_differences.values()),
        "national_mismatches": national_mismatches,
        "sex_overlaps": len(sex_differences),
        "sex_exact_matches": len(sex_differences) - len(sex_mismatches),
        "sex_maximum_difference": max(sex_differences.values(), default=Decimal(0)),
        "sex_mismatches": sex_mismatches,
    }


def prepare_temporary_csv(
    output_path: Path,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, object]],
) -> Path:
    """Write and fsync a complete temporary CSV beside its final path."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
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


def write_outputs_atomically(
    national_rows: Sequence[Mapping[str, object]],
    sex_rows: Sequence[Mapping[str, object]],
) -> None:
    """Replace outputs only after both complete temporary files are ready."""

    temporary_paths: list[Path] = []
    try:
        national_temp = prepare_temporary_csv(
            NATIONAL_OUTPUT_PATH, NATIONAL_COLUMNS, national_rows
        )
        temporary_paths.append(national_temp)
        sex_temp = prepare_temporary_csv(SEX_OUTPUT_PATH, SEX_COLUMNS, sex_rows)
        temporary_paths.append(sex_temp)
        os.replace(national_temp, NATIONAL_OUTPUT_PATH)
        temporary_paths.remove(national_temp)
        os.replace(sex_temp, SEX_OUTPUT_PATH)
        temporary_paths.remove(sex_temp)
    finally:
        for temporary_path in temporary_paths:
            if temporary_path.exists():
                temporary_path.unlink()


def print_report(
    results: Sequence[YearResult], validation: Mapping[str, object]
) -> None:
    """Print a concise retrieval and archive-validation report."""

    latest = results[-1]
    methods = sorted({result.retrieval_method for result in results})
    years = [result.year for result in results]
    print(f"Wrote {NATIONAL_OUTPUT_PATH}")
    print(f"Wrote {SEX_OUTPUT_PATH}")
    print("Retrieval succeeded: yes")
    print("Retrieval method(s): " + ", ".join(methods))
    print("Years successfully retrieved: " + ", ".join(map(str, years)))
    print(f"Latest year: {latest.year}")
    print(
        "Latest calculated national value: "
        + decimal_text(fraction_to_decimal(latest.national.calculated_fraction))
    )
    print("National archive validation:")
    print(f"  overlapping years: {validation['national_overlaps']}")
    print(f"  exact matches: {validation['national_exact_matches']}")
    print(
        "  maximum absolute difference: "
        f"{decimal_text(validation['national_maximum_difference'])}"
    )
    national_mismatches = validation["national_mismatches"]
    print(
        "  mismatching years: "
        + (
            ", ".join(map(str, national_mismatches))
            if national_mismatches
            else "none"
        )
    )
    print("Sex archive validation:")
    print(f"  overlapping rows: {validation['sex_overlaps']}")
    print(f"  exact matches: {validation['sex_exact_matches']}")
    print(
        "  maximum absolute difference: "
        f"{decimal_text(validation['sex_maximum_difference'])}"
    )
    sex_mismatches = validation["sex_mismatches"]
    print(
        "  mismatching rows: "
        + (
            ", ".join(f"{year} {sex}" for year, sex in sex_mismatches)
            if sex_mismatches
            else "none"
        )
    )
    if 2020 in years:
        print(
            "Warning: Census notes that pandemic-era response and enrollment "
            "classification issues may affect the 2020 estimate."
        )


def parse_arguments() -> argparse.Namespace:
    """Parse an inclusive survey-year range."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR)
    arguments = parser.parse_args()
    if arguments.start_year > arguments.end_year:
        parser.error("--start-year cannot be later than --end-year")
    return arguments


def main() -> None:
    arguments = parse_arguments()
    api_key = os.environ.get("CENSUS_API_KEY", "").strip() or None
    try:
        results = []
        for year in range(arguments.start_year, arguments.end_year + 1):
            records, method, source_url = retrieve_year(year, api_key)
            results.append(calculate_year(year, records, method, source_url))

        archived = read_archived_values()
        validation = validate_results(results, archived)
        national_rows, sex_rows = build_output_rows(
            results, retrieval_date=date.today().isoformat()
        )
        write_outputs_atomically(national_rows, sex_rows)
        print_report(results, validation)
    except (RetrievalError, RuntimeError, OSError) as error:
        print(
            f"Pipeline failed; existing outputs were not changed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
