"""Reusable annual natality retrieval from official CDC WONDER / NCHS NVSS.

The connector retrieves two small national tables from the current
natality database: births by medical attendant, and births plus official
female-population denominators by maternal age.  It knows CDC field names and
formats, but it deliberately does not decide what an SDG indicator means.

CDC WONDER's XML response is preferred.  If it fails, the same session-aware
official form is used to download a TSV table.  Nothing is scraped from an
unofficial site and individual birth records are never downloaded.
"""

from __future__ import annotations

import csv
import html
import http.cookiejar
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Callable, Mapping, Sequence, Tuple

from ..errors import RetrievalError, SourceValidationError
from ..http import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    PROJECT_USER_AGENT,
    request_bytes as shared_request_bytes,
)
from ..output import current_retrieval_date


WONDER_DATASET_CODE = "D66"
WONDER_DATASET_LABEL = "Natality, 2007-2024"
WONDER_FIRST_YEAR = 2007
WONDER_LAST_YEAR = 2024
WONDER_QUERY_URL = "https://wonder.cdc.gov/controller/datarequest/D66"
WONDER_PAGE_URL = "https://wonder.cdc.gov/natality-current.html"
SOURCE_ORGANIZATION = (
    "Centers for Disease Control and Prevention, "
    "National Center for Health Statistics"
)
SOURCE_DATASET = (
    "National Vital Statistics System, Natality, 2007-2024"
)
USER_AGENT = f"curl/8.7.1 {PROJECT_USER_AGENT}"
HTTP_TIMEOUT_SECONDS = DEFAULT_HTTP_TIMEOUT_SECONDS
INTER_QUERY_DELAY_SECONDS = 120.0

YEAR_VARIABLE = "D66.V20"
MEDICAL_ATTENDANT_VARIABLE = "D66.V29"
MATERNAL_AGE_9_VARIABLE = "D66.V1"
BIRTHS_MEASURE = "D66.M1"
FERTILITY_RATE_MEASURE = "D66.M5"

MEDICAL_ATTENDANT_LABELS = {
    "1": "Doctor of Medicine (MD)",
    "2": "Doctor of Osteopathy (DO)",
    "3": "Certified Nurse Midwife (CNM / CM)",
    "4": "Other Midwife",
    "5": "Other",
    "9": "Unknown or Not Stated",
}
ADOLESCENT_AGE_LABELS = {
    "15": "10-14",
    "15-19": "15-19",
}

RequestExecutor = Callable[[urllib.request.Request, str], Tuple[bytes, str]]


@dataclass(frozen=True)
class NatalityQuery:
    """One annual national natality table requested from CDC WONDER."""

    key: str
    dimension: str
    dimension_values: tuple[str, ...]
    include_fertility_rate: bool = False
    start_year: int = WONDER_FIRST_YEAR
    end_year: int = WONDER_LAST_YEAR

    @property
    def required_years(self) -> tuple[int, ...]:
        if self.start_year > self.end_year:
            raise ValueError("start_year cannot be later than end_year")
        if self.start_year < WONDER_FIRST_YEAR or self.end_year > WONDER_LAST_YEAR:
            raise ValueError(
                f"{WONDER_DATASET_CODE} supports {WONDER_FIRST_YEAR}-"
                f"{WONDER_LAST_YEAR}; received {self.start_year}-{self.end_year}"
            )
        if self.dimension not in {
            MEDICAL_ATTENDANT_VARIABLE,
            MATERNAL_AGE_9_VARIABLE,
        }:
            raise ValueError(f"Unsupported natality dimension {self.dimension!r}")
        if not self.dimension_values:
            raise ValueError("At least one dimension value is required")
        if self.include_fertility_rate and self.dimension != MATERNAL_AGE_9_VARIABLE:
            raise ValueError("Fertility rates require the maternal-age dimension")
        return tuple(range(self.start_year, self.end_year + 1))


@dataclass(frozen=True)
class NatalityObservation:
    """One validated year/category observation from a WONDER result."""

    year: int
    category_code: str
    category_label: str
    births: int | None
    female_population: int | None
    source_reported_fertility_rate: Decimal | None
    suppression_status: str
    source_notes: tuple[str, ...]


@dataclass(frozen=True)
class NvssNatalityResult:
    """A coordinated natality batch and the successful source provenance."""

    observations: Mapping[str, tuple[NatalityObservation, ...]]
    source_organization: str
    source_dataset: str
    source_url: str
    retrieval_method: str
    retrieval_date: str
    source_warnings: tuple[str, ...] = ()
    births_source_url: str = ""
    population_source_url: str = ""


BatchFetcher = Callable[[Sequence[NatalityQuery], str], NvssNatalityResult]

# The current public-use files retain these reviewed fixed-width fields across
# 2022-2024. Keeping the tiny layout explicit makes future-year review visible.
PUBLIC_USE_LAYOUTS = {
    year: {
        "url": (
            "https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Datasets/DVS/"
            f"natality/Nat{year}us.zip"
        ),
        "maternal_age": (74, 76),       # 1-based positions 75-76: MAGER
        "residence_status": (103, 104), # position 104: RESTATUS
        "attendant": (432, 433),        # position 433: ATTEND
    }
    for year in range(2022, 2025)
}
POPULATION_URLS = {
    year: (
        "https://www2.census.gov/programs-surveys/popest/datasets/"
        f"2020-{year}/national/asrh/nc-est{year}-agesex-res.csv"
    )
    for year in range(2022, 2025)
}


class _WonderFormParser(HTMLParser):
    """Collect successful controls from WONDER's current request form."""

    def __init__(self) -> None:
        super().__init__()
        self.in_form = False
        self.action = ""
        self.pairs: list[tuple[str, str]] = []
        self.select_name: str | None = None
        self.select_options: list[tuple[str, bool]] = []
        self.textarea_name: str | None = None
        self.textarea_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "form" and f"datarequest/{WONDER_DATASET_CODE}" in values.get("action", ""):
            self.in_form = True
            self.action = values["action"]
        elif self.in_form and tag == "input":
            name = values.get("name")
            input_type = values.get("type", "text").lower()
            disabled = any(name == "disabled" for name, _value in attrs)
            checked = any(name == "checked" for name, _value in attrs)
            if (
                name
                and not disabled
                and input_type not in {"button", "file", "image", "reset", "submit"}
                and (input_type not in {"checkbox", "radio"} or checked)
            ):
                self.pairs.append((name, values.get("value", "")))
        elif self.in_form and tag == "select":
            self.select_name = values.get("name")
            self.select_options = []
        elif self.in_form and tag == "option" and self.select_name:
            selected = any(name == "selected" for name, _value in attrs)
            self.select_options.append((values.get("value", ""), selected))
        elif self.in_form and tag == "textarea":
            self.textarea_name = values.get("name")
            self.textarea_parts = []

    def handle_data(self, data: str) -> None:
        if self.textarea_name:
            self.textarea_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self.select_name:
            chosen = [value for value, selected in self.select_options if selected]
            if not chosen and self.select_options:
                chosen = [self.select_options[0][0]]
            self.pairs.extend((self.select_name, value) for value in chosen)
            self.select_name = None
            self.select_options = []
        elif tag == "textarea" and self.textarea_name:
            self.pairs.append((self.textarea_name, "".join(self.textarea_parts)))
            self.textarea_name = None
            self.textarea_parts = []
        elif tag == "form" and self.in_form:
            self.in_form = False


def _cookie_request_executor() -> RequestExecutor:
    """Create one executor whose cookie jar survives the consent/query sequence."""

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def execute(request: urllib.request.Request, display_url: str) -> Tuple[bytes, str]:
        return shared_request_bytes(
            request,
            display_url=display_url,
            timeout=HTTP_TIMEOUT_SECONDS,
            open_request=opener.open,
        )

    return execute


def _read_form(request_executor: RequestExecutor) -> tuple[str, list[tuple[str, str]]]:
    request = urllib.request.Request(
        WONDER_QUERY_URL,
        data=urllib.parse.urlencode(
            {"stage": "about", "action-I Agree": "I Agree"}
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    body, content_type = request_executor(request, WONDER_PAGE_URL)
    if content_type != "text/html":
        raise SourceValidationError(
            f"CDC WONDER consent returned unexpected content type {content_type!r}"
        )
    parser = _WonderFormParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    if not parser.action or not parser.pairs:
        raise SourceValidationError(
            "CDC WONDER did not provide a complete natality request form"
        )
    return urllib.parse.urljoin(WONDER_PAGE_URL, html.unescape(parser.action)), parser.pairs


def _replace(
    grouped: dict[str, list[str]], name: str, values: str | Sequence[str]
) -> None:
    grouped[name] = [values] if isinstance(values, str) else list(values)


def wonder_parameters(
    query: NatalityQuery, defaults: Sequence[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Apply a small reviewed query override to WONDER's current form defaults."""

    grouped: dict[str, list[str]] = defaultdict(list)
    order: list[str] = []
    for name, value in defaults:
        if name not in grouped:
            order.append(name)
        grouped[name].append(value)
    for name in list(grouped):
        if name.startswith(("B_", "M_", "action-")):
            del grouped[name]
    for name in ("B_1", "B_2", "B_3", "B_4", "B_5", "M_1", "M_2"):
        if name not in order:
            order.append(name)

    _replace(grouped, "B_1", f"{YEAR_VARIABLE}-level1")
    _replace(grouped, "B_2", query.dimension)
    _replace(grouped, "B_3", "*None*")
    _replace(grouped, "B_4", "*None*")
    _replace(grouped, "B_5", "*None*")
    _replace(grouped, "M_1", BIRTHS_MEASURE)
    if query.include_fertility_rate:
        _replace(grouped, "M_2", FERTILITY_RATE_MEASURE)
    else:
        grouped.pop("M_2", None)
    _replace(grouped, f"V_{YEAR_VARIABLE}", [str(year) for year in query.required_years])
    _replace(grouped, f"V_{query.dimension}", query.dimension_values)
    _replace(grouped, "O_javascript", "off")
    _replace(grouped, "O_show_suppressed", "true")
    _replace(grouped, "O_show_totals", "true")
    _replace(grouped, "O_show_zeros", "true")
    _replace(grouped, "O_precision", "9")
    _replace(grouped, "O_timeout", "600")
    _replace(grouped, "stage", "request")
    _replace(grouped, "accept_datause_restrictions", "true")

    pairs: list[tuple[str, str]] = []
    for name in order:
        pairs.extend((name, value) for value in grouped.get(name, ()))
    for name, values in grouped.items():
        if name not in order:
            pairs.extend((name, value) for value in values)
    return pairs


def build_wonder_request_xml(
    query: NatalityQuery, defaults: Sequence[tuple[str, str]]
) -> bytes:
    root = ET.Element("request-parameters")
    grouped: dict[str, list[str]] = defaultdict(list)
    for name, value in wonder_parameters(query, defaults):
        grouped[name].append(value)
    for name, values in grouped.items():
        parameter = ET.SubElement(root, "parameter")
        ET.SubElement(parameter, "name").text = name
        for value in values:
            ET.SubElement(parameter, "value").text = value
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _cell_value(cell: ET.Element) -> str:
    for attribute in ("v", "dt", "l"):
        if attribute in cell.attrib:
            return cell.attrib[attribute].strip()
    return "".join(cell.itertext()).strip()


def _cell_label(cell: ET.Element) -> str:
    for attribute in ("l", "v", "dt"):
        if attribute in cell.attrib:
            return cell.attrib[attribute].strip()
    return "".join(cell.itertext()).strip()


def _parse_int(text: str, field: str, year: int) -> int:
    try:
        return int(text.replace(",", "").strip())
    except ValueError as error:
        raise SourceValidationError(
            f"Invalid CDC WONDER {field} for {year}: {text!r}"
        ) from error


def _parse_decimal(text: str, field: str, year: int) -> Decimal:
    try:
        value = Decimal(text.replace(",", "").strip())
    except InvalidOperation as error:
        raise SourceValidationError(
            f"Invalid CDC WONDER {field} for {year}: {text!r}"
        ) from error
    if not value.is_finite():
        raise SourceValidationError(
            f"Non-finite CDC WONDER {field} for {year}: {text!r}"
        )
    return value


def _is_suppressed(values: Sequence[str]) -> bool:
    combined = " ".join(values).lower()
    return any(marker in combined for marker in ("suppressed", "unreliable", "---"))


def _category_label(query: NatalityQuery, code: str, source_label: str) -> str:
    expected = (
        MEDICAL_ATTENDANT_LABELS
        if query.dimension == MEDICAL_ATTENDANT_VARIABLE
        else ADOLESCENT_AGE_LABELS
    )
    if code not in expected:
        raise SourceValidationError(
            f"CDC WONDER returned unexpected category {code!r} for {query.key}"
        )
    return source_label or expected[code]


def _validate_rows(
    observations: Sequence[NatalityObservation], query: NatalityQuery
) -> None:
    keys = [(row.year, row.category_code) for row in observations]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise SourceValidationError(
            f"CDC WONDER returned duplicate natality rows for {query.key}: {duplicates}"
        )
    expected = {
        (year, category)
        for year in query.required_years
        for category in query.dimension_values
    }
    missing = sorted(expected - set(keys))
    if missing:
        raise SourceValidationError(
            f"CDC WONDER is missing required natality observations for {query.key}: "
            + ", ".join(f"{year}/{category}" for year, category in missing)
        )


def parse_wonder_xml(
    body: bytes, query: NatalityQuery
) -> tuple[NatalityObservation, ...]:
    """Parse births, and where requested population/rate, from WONDER XML."""

    if body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise SourceValidationError("CDC WONDER returned HTML instead of XML data")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise SourceValidationError("CDC WONDER returned malformed XML") from error
    if root.tag != "page":
        raise SourceValidationError(
            f"CDC WONDER XML has unexpected root element {root.tag!r}"
        )
    errors = [
        " ".join(element.itertext()).strip()
        for element in root.findall(".//error-message")
        if " ".join(element.itertext()).strip()
    ]
    if errors:
        raise SourceValidationError("CDC WONDER rejected the query: " + "; ".join(errors))
    table = root.find(".//data-table")
    if table is None:
        raise SourceValidationError("CDC WONDER XML contains no data table")

    observations: list[NatalityObservation] = []
    measure_count = 3 if query.include_fertility_rate else 1
    for row in table.findall("r"):
        cells = row.findall("c")
        if len(cells) < 2 + measure_count:
            continue
        year_text = _cell_value(cells[0])
        if not year_text.isdigit():
            continue
        year = int(year_text)
        category_code = _cell_value(cells[1])
        category_label = _category_label(query, category_code, _cell_label(cells[1]))
        values = [_cell_value(cell) for cell in cells[2 : 2 + measure_count]]
        suppressed = _is_suppressed(values) or not values[0]
        births = None if suppressed else _parse_int(values[0], "births", year)
        population = None
        reported_rate = None
        if query.include_fertility_rate and not suppressed:
            population = _parse_int(values[1], "female population", year)
            reported_rate = _parse_decimal(values[2], "fertility rate", year)
        observations.append(
            NatalityObservation(
                year=year,
                category_code=category_code,
                category_label=category_label,
                births=births,
                female_population=population,
                source_reported_fertility_rate=reported_rate,
                suppression_status="suppressed" if suppressed else "not_suppressed",
                source_notes=(
                    "Final resident live births from birth certificates.",
                    *(
                        (
                            "Female population is the annual Census denominator supplied "
                            "by CDC WONDER for the maternal-age fertility rate.",
                        )
                        if query.include_fertility_rate
                        else ()
                    ),
                ),
            )
        )
    _validate_rows(observations, query)
    return tuple(sorted(observations, key=lambda item: (item.year, item.category_code)))


def _find_column(header: Sequence[str], *candidates: str) -> int:
    normalized = {value.strip().lower(): index for index, value in enumerate(header)}
    for candidate in candidates:
        if candidate.lower() in normalized:
            return normalized[candidate.lower()]
    raise SourceValidationError(
        "CDC WONDER TSV is missing column: " + " or ".join(candidates)
    )


def parse_wonder_tsv(
    body: bytes, query: NatalityQuery
) -> tuple[NatalityObservation, ...]:
    """Parse the equivalent official CDC WONDER TSV export."""

    if body.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<?xml")):
        raise SourceValidationError("CDC WONDER returned markup instead of TSV data")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SourceValidationError("CDC WONDER TSV is not valid UTF-8") from error
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t")
    try:
        header = next(reader)
    except StopIteration as error:
        raise SourceValidationError("CDC WONDER TSV is empty") from error

    year_index = _find_column(header, "Year Code", "Year")
    if query.dimension == MEDICAL_ATTENDANT_VARIABLE:
        label_index = _find_column(header, "Medical Attendant")
        code_index = _find_column(header, "Medical Attendant Code")
    else:
        label_index = _find_column(header, "Age of Mother 9")
        code_index = _find_column(header, "Age of Mother 9 Code")
    births_index = _find_column(header, "Births")
    population_index = (
        _find_column(header, "Population") if query.include_fertility_rate else None
    )
    rate_index = (
        _find_column(header, "Fertility Rate") if query.include_fertility_rate else None
    )

    observations: list[NatalityObservation] = []
    for raw_row in reader:
        if not raw_row or raw_row[0].strip() in {"---", "Total", "Caveats:"}:
            break
        row = raw_row + [""] * (len(header) - len(raw_row))
        year_text = row[year_index].strip()
        if not year_text.isdigit():
            continue
        year = int(year_text)
        category_code = row[code_index].strip()
        category_label = _category_label(query, category_code, row[label_index].strip())
        values = [row[births_index]]
        if population_index is not None and rate_index is not None:
            values.extend([row[population_index], row[rate_index]])
        suppressed = _is_suppressed(values) or not values[0].strip()
        observations.append(
            NatalityObservation(
                year=year,
                category_code=category_code,
                category_label=category_label,
                births=None if suppressed else _parse_int(values[0], "births", year),
                female_population=(
                    None
                    if suppressed or population_index is None
                    else _parse_int(values[1], "female population", year)
                ),
                source_reported_fertility_rate=(
                    None
                    if suppressed or rate_index is None
                    else _parse_decimal(values[2], "fertility rate", year)
                ),
                suppression_status="suppressed" if suppressed else "not_suppressed",
                source_notes=("Final resident live births from birth certificates.",),
            )
        )
    _validate_rows(observations, query)
    return tuple(sorted(observations, key=lambda item: (item.year, item.category_code)))


def _validate_batch(
    observations: Mapping[str, tuple[NatalityObservation, ...]],
    queries: Sequence[NatalityQuery],
) -> None:
    if set(observations) != {query.key for query in queries}:
        raise SourceValidationError("Natality batch keys do not match requested queries")
    for query in queries:
        _validate_rows(observations[query.key], query)


def fetch_from_wonder_api(
    queries: Sequence[NatalityQuery],
    retrieval_date: str,
    *,
    request_executor: RequestExecutor | None = None,
    initial_query_delay_seconds: float = INTER_QUERY_DELAY_SECONDS,
    inter_query_delay_seconds: float = INTER_QUERY_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> NvssNatalityResult:
    """Use CDC's XML response with current form defaults and a cookie session."""

    executor = request_executor or _cookie_request_executor()
    session_url, defaults = _read_form(executor)
    # The consent form is itself a WONDER request. Spacing it from the first
    # data query follows CDC's published data-mining guidance and avoids 429s.
    if initial_query_delay_seconds:
        sleep(initial_query_delay_seconds)
    observations: dict[str, tuple[NatalityObservation, ...]] = {}
    for query_number, query in enumerate(queries):
        encoded = urllib.parse.urlencode(
            {
                "request_xml": build_wonder_request_xml(query, defaults).decode("utf-8"),
                "accept_datause_restrictions": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            session_url,
            data=encoded,
            headers={
                "Accept": "application/xml,text/xml",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        body, content_type = executor(request, WONDER_QUERY_URL)
        if content_type not in {"application/xml", "text/xml", "text/plain", "text/html"}:
            raise SourceValidationError(
                f"CDC WONDER API returned unexpected content type {content_type!r}"
            )
        observations[query.key] = parse_wonder_xml(body, query)
        if query_number < len(queries) - 1 and inter_query_delay_seconds:
            sleep(inter_query_delay_seconds)
    _validate_batch(observations, queries)
    return NvssNatalityResult(
        observations=observations,
        source_organization=SOURCE_ORGANIZATION,
        source_dataset=SOURCE_DATASET,
        source_url=WONDER_QUERY_URL,
        retrieval_method="cdc_wonder_api",
        retrieval_date=retrieval_date,
        births_source_url=WONDER_QUERY_URL,
        population_source_url=WONDER_QUERY_URL,
    )


def fetch_from_wonder_tsv(
    queries: Sequence[NatalityQuery],
    retrieval_date: str,
    *,
    request_executor: RequestExecutor | None = None,
    initial_query_delay_seconds: float = INTER_QUERY_DELAY_SECONDS,
    inter_query_delay_seconds: float = INTER_QUERY_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> NvssNatalityResult:
    """Fallback to the official session-aware WONDER TSV export."""

    executor = request_executor or _cookie_request_executor()
    session_url, defaults = _read_form(executor)
    if initial_query_delay_seconds:
        sleep(initial_query_delay_seconds)
    observations: dict[str, tuple[NatalityObservation, ...]] = {}
    for query_number, query in enumerate(queries):
        parameters = wonder_parameters(query, defaults)
        parameters.append(("action-Send", "Send"))
        request = urllib.request.Request(
            session_url,
            data=urllib.parse.urlencode(parameters).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        result_body, result_type = executor(request, WONDER_PAGE_URL)
        if result_type != "text/html" or b" Results" not in result_body:
            raise SourceValidationError(f"CDC WONDER web query failed for {query.key}")
        export_request = urllib.request.Request(
            session_url,
            data=urllib.parse.urlencode(
                {
                    "stage": "results",
                    "O_export-format": "tsv",
                    "action-Export": "Download",
                }
            ).encode("utf-8"),
            headers={
                "Accept": "text/tab-separated-values,text/plain",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        body, content_type = executor(export_request, WONDER_PAGE_URL)
        if content_type not in {
            "text/plain",
            "text/tab-separated-values",
            "application/octet-stream",
            "text/html",
        }:
            raise SourceValidationError(
                f"CDC WONDER export returned unexpected content type {content_type!r}"
            )
        observations[query.key] = parse_wonder_tsv(body, query)
        if query_number < len(queries) - 1 and inter_query_delay_seconds:
            sleep(inter_query_delay_seconds)
    _validate_batch(observations, queries)
    return NvssNatalityResult(
        observations=observations,
        source_organization=SOURCE_ORGANIZATION,
        source_dataset=SOURCE_DATASET,
        source_url=WONDER_PAGE_URL,
        retrieval_method="cdc_wonder_tsv_fallback",
        retrieval_date=retrieval_date,
        source_warnings=(
            "The XML API was unavailable; the official CDC WONDER TSV export was used.",
        ),
        births_source_url=WONDER_PAGE_URL,
        population_source_url=WONDER_PAGE_URL,
    )


def _public_use_rows(body: bytes, year: int) -> tuple[dict[str, int], dict[str, int]]:
    """Stream one official ZIP member and aggregate only required fields."""

    layout = PUBLIC_USE_LAYOUTS[year]
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except (zipfile.BadZipFile, OSError) as error:
        raise SourceValidationError(
            f"NCHS {year} natality public-use download is not a valid ZIP"
        ) from error
    members = [info for info in archive.infolist() if not info.is_dir()]
    if len(members) != 1:
        raise SourceValidationError(
            f"NCHS {year} natality ZIP should contain exactly one data file"
        )
    minimum_length = max(end for start, end in (
        layout["maternal_age"], layout["residence_status"], layout["attendant"]
    ))

    def aggregate(raw_file: object) -> tuple[dict[str, int], dict[str, int]]:
        attendants = {code: 0 for code in MEDICAL_ATTENDANT_LABELS}
        ages = {code: 0 for code in ADOLESCENT_AGE_LABELS}
        record_count = 0
        for line_number, raw_line in enumerate(raw_file, start=1):
            line = raw_line.rstrip(b"\r\n")
            if len(line) < minimum_length:
                raise SourceValidationError(
                    f"NCHS {year} natality record {line_number} is too short"
                )
            residence = line[slice(*layout["residence_status"])].decode("ascii")
            if residence == "4":
                continue  # The SDG geography is U.S. residents.
            attendant = line[slice(*layout["attendant"])].decode("ascii")
            maternal_age = line[slice(*layout["maternal_age"])].decode("ascii")
            if attendant not in attendants:
                raise SourceValidationError(
                    f"NCHS {year} contains unexpected ATTEND code {attendant!r}"
                )
            attendants[attendant] += 1
            try:
                age = int(maternal_age)
            except ValueError as error:
                raise SourceValidationError(
                    f"NCHS {year} contains invalid MAGER value {maternal_age!r}"
                ) from error
            if 10 <= age <= 14:
                ages["15"] += 1
            elif 15 <= age <= 19:
                ages["15-19"] += 1
            record_count += 1
        if record_count <= 0 or sum(attendants.values()) != record_count:
            raise SourceValidationError(
                f"NCHS {year} natality file contains no resident births"
            )
        return attendants, ages

    try:
        with archive, archive.open(members[0]) as raw_file:
            return aggregate(raw_file)
    except NotImplementedError:
        # NCHS currently uses Deflate64 (ZIP method 9), which Python's standard
        # library cannot decode. The platform's standard unzip supports it. The ZIP
        # is temporary and streamed to stdout; no microdata are extracted.
        archive_reader = shutil.which("unzip")
        if not archive_reader:
            raise SourceValidationError(
                "NCHS public-use ZIP uses unsupported Deflate64 compression "
                "and no unzip archive reader is available"
            )
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temporary:
                temporary_path = temporary.name
                temporary.write(body)
            process = subprocess.Popen(
                [archive_reader, "-p", temporary_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if process.stdout is None:
                raise SourceValidationError("Could not stream the NCHS public-use ZIP")
            try:
                result = aggregate(process.stdout)
            finally:
                process.stdout.close()
            stderr = process.stderr.read().decode("utf-8", errors="replace")
            return_code = process.wait()
            if return_code != 0:
                raise SourceValidationError(
                    f"Could not read NCHS {year} public-use ZIP: {stderr.strip()}"
                )
            return result
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass


def _population_rows(body: bytes, year: int) -> dict[str, int]:
    """Sum female single-year Census estimates into the two SDG age groups."""

    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SourceValidationError(
            f"Census {year} population file is not valid UTF-8"
        ) from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {"SEX", "AGE", f"POPESTIMATE{year}"}
    missing = sorted(required - set(reader.fieldnames or ()))
    if missing:
        raise SourceValidationError(
            f"Census {year} population file is missing: " + ", ".join(missing)
        )
    populations = {"15": 0, "15-19": 0}
    seen_ages: set[int] = set()
    for row in reader:
        if row["SEX"] != "2":
            continue
        try:
            age = int(row["AGE"])
            population = int(row[f"POPESTIMATE{year}"])
        except (ValueError, TypeError) as error:
            raise SourceValidationError(
                f"Census {year} population file contains invalid numeric data"
            ) from error
        if 10 <= age <= 19:
            if age in seen_ages:
                raise SourceValidationError(
                    f"Census {year} population file duplicates female age {age}"
                )
            seen_ages.add(age)
            populations["15" if age <= 14 else "15-19"] += population
    if seen_ages != set(range(10, 20)) or any(value <= 0 for value in populations.values()):
        raise SourceValidationError(
            f"Census {year} population file lacks complete female ages 10-19"
        )
    return populations


def fetch_from_public_use(
    queries: Sequence[NatalityQuery],
    retrieval_date: str,
    *,
    request_executor: RequestExecutor = lambda request, url: shared_request_bytes(
        request, display_url=url, timeout=HTTP_TIMEOUT_SECONDS
    ),
) -> NvssNatalityResult:
    """Use official annual NCHS microdata plus Census denominator CSVs."""

    years = sorted({year for query in queries for year in query.required_years})
    unsupported = sorted(set(years) - set(PUBLIC_USE_LAYOUTS))
    if unsupported:
        raise SourceValidationError(
            "Public-use fallback has no reviewed layout for years: "
            + ", ".join(map(str, unsupported))
        )
    by_year: dict[int, tuple[dict[str, int], dict[str, int], dict[str, int]]] = {}
    for year in years:
        natality_url = PUBLIC_USE_LAYOUTS[year]["url"]
        request = urllib.request.Request(
            natality_url,
            # NCHS's FTP gateway returns 406 for a narrow ZIP Accept header.
            headers={"User-Agent": USER_AGENT},
        )
        natality_body, natality_type = request_executor(request, natality_url)
        if natality_type not in {
            "application/zip",
            "application/x-zip-compressed",
            "application/octet-stream",
        }:
            raise SourceValidationError(
                f"NCHS {year} public-use file has unexpected content type {natality_type!r}"
            )
        attendants, ages = _public_use_rows(natality_body, year)
        population_url = POPULATION_URLS[year]
        population_request = urllib.request.Request(
            population_url,
            headers={"Accept": "text/csv,text/plain", "User-Agent": USER_AGENT},
        )
        population_body, _population_type = request_executor(
            population_request, population_url
        )
        by_year[year] = attendants, ages, _population_rows(population_body, year)

    observations: dict[str, tuple[NatalityObservation, ...]] = {}
    for query in queries:
        rows: list[NatalityObservation] = []
        for year in query.required_years:
            attendants, ages, populations = by_year[year]
            counts = attendants if query.dimension == MEDICAL_ATTENDANT_VARIABLE else ages
            for code in query.dimension_values:
                population = populations[code] if query.include_fertility_rate else None
                births = counts[code]
                rows.append(
                    NatalityObservation(
                        year=year,
                        category_code=code,
                        category_label=_category_label(query, code, ""),
                        births=births,
                        female_population=population,
                        source_reported_fertility_rate=None,
                        suppression_status="not_suppressed",
                        source_notes=(
                            "Final NCHS resident natality public-use file.",
                            *(
                                ("Census Population Estimates Program female denominator.",)
                                if query.include_fertility_rate else ()
                            ),
                        ),
                    )
                )
        _validate_rows(rows, query)
        observations[query.key] = tuple(
            sorted(rows, key=lambda item: (item.year, item.category_code))
        )
    urls = [PUBLIC_USE_LAYOUTS[year]["url"] for year in years]
    population_urls = [POPULATION_URLS[year] for year in years]
    return NvssNatalityResult(
        observations=observations,
        source_organization=SOURCE_ORGANIZATION,
        source_dataset="National Vital Statistics System, Natality public-use files",
        source_url=" | ".join((*urls, *population_urls)),
        retrieval_method="nchs_public_use_and_census_download",
        retrieval_date=retrieval_date,
        source_warnings=(
            "CDC WONDER query paths were unavailable; official NCHS public-use "
            "files and matching Census population estimates were used.",
        ),
        births_source_url=" | ".join(urls),
        population_source_url=" | ".join(population_urls),
    )


def fetch_natality_batch(
    queries: Sequence[NatalityQuery],
    *,
    retrieval_date: str | None = None,
    api_fetcher: BatchFetcher | None = None,
    fallback_fetcher: BatchFetcher | None = None,
    public_use_fetcher: BatchFetcher | None = None,
) -> NvssNatalityResult:
    """Retrieve a coordinated batch, preserving API-to-official-TSV fallback."""

    if not queries:
        raise ValueError("At least one natality query is required")
    keys = [query.key for query in queries]
    if len(keys) != len(set(keys)):
        raise ValueError("Natality query keys must be unique")
    run_date = retrieval_date or current_retrieval_date()
    primary = api_fetcher or (lambda requested, date: fetch_from_wonder_api(requested, date))
    fallback = fallback_fetcher or (
        lambda requested, date: fetch_from_wonder_tsv(requested, date)
    )
    public_use = public_use_fetcher or (
        lambda requested, date: fetch_from_public_use(requested, date)
    )
    try:
        result = primary(queries, run_date)
    except (RetrievalError, SourceValidationError) as api_error:
        try:
            result = fallback(queries, run_date)
        except (RetrievalError, SourceValidationError) as fallback_error:
            try:
                result = public_use(queries, run_date)
            except (RetrievalError, SourceValidationError) as public_use_error:
                raise RetrievalError(
                    "All official natality source paths failed: "
                    f"WONDER API: {api_error}; WONDER TSV: {fallback_error}; "
                    f"public use: {public_use_error}"
                ) from public_use_error
    _validate_batch(result.observations, queries)
    return result
