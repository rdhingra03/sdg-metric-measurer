"""Reusable national mortality retrieval from CDC WONDER / NCHS NVSS.

The connector knows how to ask CDC WONDER for annual deaths, population, and
crude rates for an arbitrary ICD-10 selection.  It deliberately does not know
what any SDG indicator means.  Indicator modules supply the ICD selection and
interpret the returned counts.

CDC's documented XML API is preferred.  If that endpoint is temporarily
unavailable, the connector uses the same official WONDER query through its
session-aware web form and downloads CDC's TSV export.  Both paths apply
WONDER's suppression rules; suppressed cells are never reconstructed.
"""

from __future__ import annotations

import csv
import html
import io
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Sequence, Tuple

from ..errors import RetrievalError, SourceValidationError
from ..http import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    PROJECT_USER_AGENT,
    request_bytes as shared_request_bytes,
)
from ..output import current_retrieval_date


WONDER_DATASET_CODE = "D158"
WONDER_DATASET_LABEL = "Underlying Cause of Death, 2018-2024, Single Race"
WONDER_DATASET_VINTAGE = "2024"
WONDER_FIRST_YEAR = 2018
WONDER_LAST_YEAR = 2024
WONDER_QUERY_URL = (
    "https://wonder.cdc.gov/controller/datarequest/" + WONDER_DATASET_CODE
)
WONDER_PAGE_URL = "https://wonder.cdc.gov/ucd-icd10-expanded.html"
SOURCE_ORGANIZATION = (
    "Centers for Disease Control and Prevention, "
    "National Center for Health Statistics"
)
SOURCE_DATASET = (
    "National Vital Statistics System, Underlying Cause of Death, 2018-2024"
)
HTTP_TIMEOUT_SECONDS = DEFAULT_HTTP_TIMEOUT_SECONDS
# CDC's web-application firewall rejects some otherwise valid custom user-agent
# shapes.  A curl-compatible prefix plus the project identifier is accepted and
# still identifies this client honestly in server logs.
USER_AGENT = f"curl/8.7.1 {PROJECT_USER_AGENT}"
DEFAULT_SOURCE_NOTE = (
    "Final resident mortality counts from death certificates; population is "
    "the annual resident-population denominator supplied by CDC WONDER."
)
# CDC's published API guidance recommends spacing data-mining queries about
# two minutes apart.  The batch makes only three serialized national queries,
# so following that guidance adds a few minutes but avoids overloading WONDER.
INTER_QUERY_DELAY_SECONDS = 120.0

RequestExecutor = Callable[[urllib.request.Request, str], Tuple[bytes, str]]


@dataclass(frozen=True)
class MortalityQuery:
    """One national annual mortality request identified by an arbitrary key."""

    key: str
    icd10_selection: tuple[str, ...]
    start_year: int = WONDER_FIRST_YEAR
    end_year: int = WONDER_LAST_YEAR

    @property
    def required_years(self) -> tuple[int, ...]:
        """Return the inclusive annual range required from the source."""

        if self.start_year > self.end_year:
            raise ValueError("start_year cannot be later than end_year")
        if self.start_year < WONDER_FIRST_YEAR or self.end_year > WONDER_LAST_YEAR:
            raise ValueError(
                f"{WONDER_DATASET_CODE} supports {WONDER_FIRST_YEAR}-"
                f"{WONDER_LAST_YEAR}; received {self.start_year}-{self.end_year}"
            )
        if not self.icd10_selection:
            raise ValueError("At least one ICD-10 selector is required")
        return tuple(range(self.start_year, self.end_year + 1))

    @property
    def icd10_text(self) -> str:
        """Return the stable audit representation of the requested selection."""

        return "; ".join(self.icd10_selection)


@dataclass(frozen=True)
class MortalityObservation:
    """One validated annual national mortality observation."""

    year: int
    deaths: int | None
    population: int | None
    crude_rate: Decimal | None
    source_reported_crude_rate: Decimal | None
    icd10_selection: tuple[str, ...]
    disaggregation: Mapping[str, str]
    suppression_status: str
    source_notes: tuple[str, ...]


@dataclass(frozen=True)
class NvssMortalityResult:
    """A coordinated batch plus the provenance of the successful source path."""

    observations: Mapping[str, tuple[MortalityObservation, ...]]
    source_organization: str
    source_dataset: str
    source_url: str
    retrieval_method: str
    retrieval_date: str
    source_warnings: tuple[str, ...] = ()


BatchFetcher = Callable[
    [Sequence[MortalityQuery], str], NvssMortalityResult
]


def request_bytes(
    request: urllib.request.Request, display_url: str
) -> Tuple[bytes, str]:
    """Execute one credential-safe CDC request with shared retry behavior."""

    return shared_request_bytes(
        request,
        display_url=display_url,
        timeout=HTTP_TIMEOUT_SECONDS,
    )


def calculate_crude_rate(deaths: int, population: int) -> Decimal:
    """Calculate deaths per 100,000 without presentation rounding."""

    if deaths < 0:
        raise SourceValidationError("Mortality deaths cannot be negative")
    if population <= 0:
        raise SourceValidationError("Mortality population must be positive")
    return Decimal(deaths) * Decimal(100_000) / Decimal(population)


def _add_parameter(
    root: ET.Element, name: str, values: str | Sequence[str]
) -> None:
    parameter = ET.SubElement(root, "parameter")
    ET.SubElement(parameter, "name").text = name
    if isinstance(values, str):
        values = (values,)
    for value in values:
        ET.SubElement(parameter, "value").text = value


def wonder_parameters(query: MortalityQuery) -> list[tuple[str, str]]:
    """Return the complete, verified D158 parameter set for one query.

    CDC WONDER changes database identifiers when a new mortality vintage is
    released.  Keeping the database-specific parameters together makes that
    future update explicit and reviewable.
    """

    years = tuple(str(year) for year in query.required_years)
    pairs: list[tuple[str, str]] = [
        ("B_1", "D158.V1-level1"),
        ("B_2", "*None*"),
        ("B_3", "*None*"),
        ("B_4", "*None*"),
        ("B_5", "*None*"),
    ]
    pairs.extend(("F_D158.V1", year) for year in years)
    for name in (
        "F_D158.V10",
        "F_D158.V2",
        "F_D158.V25",
        "F_D158.V27",
        "F_D158.V30",
        "F_D158.V31",
        "F_D158.V9",
    ):
        pairs.append((name, "*All*"))
    pairs.extend(
        [
            ("I_D158.V1", "\n".join(f"{year} ({year})" for year in years)),
            ("I_D158.V10", "*All* (The United States)"),
            ("I_D158.V2", query.icd10_text),
            ("I_D158.V25", "All Causes of Death"),
            ("I_D158.V27", "*All* (The United States)"),
            ("I_D158.V30", "*All* (The United States)"),
            ("I_D158.V31", "*All* (The United States)"),
            ("I_D158.V9", "*All* (The United States)"),
            ("M_1", "D158.M1"),
            ("M_2", "D158.M2"),
            ("M_3", "D158.M3"),
        ]
    )
    for name in (
        "O_V10_fmode",
        "O_V1_fmode",
        "O_V25_fmode",
        "O_V27_fmode",
        "O_V30_fmode",
        "O_V31_fmode",
        "O_V9_fmode",
    ):
        pairs.append((name, "freg"))
    pairs.extend(
        [
            ("O_V2_fmode", "fadv"),
            ("O_aar", "aar_none"),
            ("O_aar_pop", "0000"),
            ("O_age", "D158.V5"),
            ("O_export-format", "tsv"),
            ("O_javascript", "off"),
            ("O_location", "D158.V9"),
            ("O_oc-sect1-request", "close"),
            ("O_precision", "9"),
            ("O_race", "D158.V42"),
            ("O_rate_per", "100000"),
            ("O_show_suppressed", "true"),
            ("O_show_totals", "true"),
            ("O_show_zeros", "true"),
            ("O_timeout", "600"),
            ("O_title", ""),
            ("O_ucd", "D158.V2"),
            ("O_urban", "D158.V19"),
            ("VM_D158.M6_D158.V10", ""),
            ("VM_D158.M6_D158.V17", "*All*"),
            ("VM_D158.M6_D158.V1_S", "*All*"),
            ("VM_D158.M6_D158.V42", "*All*"),
            ("VM_D158.M6_D158.V7", "*All*"),
            ("V_D158.V1", ""),
            ("V_D158.V10", ""),
        ]
    )
    for name in (
        "V_D158.V11",
        "V_D158.V12",
        "V_D158.V17",
        "V_D158.V18",
        "V_D158.V19",
    ):
        pairs.append((name, "*All*"))
    pairs.append(("V_D158.V2", "\n".join(query.icd10_selection)))
    for name in (
        "V_D158.V20",
        "V_D158.V21",
        "V_D158.V22",
        "V_D158.V23",
        "V_D158.V24",
    ):
        pairs.append((name, "*All*"))
    pairs.extend(
        [
            ("V_D158.V25", ""),
            ("V_D158.V27", ""),
            ("V_D158.V30", ""),
            ("V_D158.V31", ""),
        ]
    )
    for name in (
        "V_D158.V4",
        "V_D158.V42",
        "V_D158.V43",
        "V_D158.V44",
        "V_D158.V45",
        "V_D158.V5",
        "V_D158.V51",
        "V_D158.V52",
    ):
        pairs.append((name, "*All*"))
    pairs.extend(
        [
            ("V_D158.V6", "00"),
            ("V_D158.V7", "*All*"),
            ("V_D158.V9", ""),
            ("action-Send", "Send"),
            ("dataset_code", WONDER_DATASET_CODE),
            ("dataset_label", WONDER_DATASET_LABEL),
            ("dataset_vintage", WONDER_DATASET_VINTAGE),
        ]
    )
    for variable in ("V1", "V10", "V2", "V25", "V27", "V30", "V31", "V9"):
        pairs.append((f"finder-stage-D158.{variable}", "codeset"))
    pairs.extend(
        [
            ("saved_id", ""),
            ("stage", "request"),
            ("accept_datause_restrictions", "true"),
        ]
    )
    return pairs


def build_wonder_request_xml(query: MortalityQuery) -> bytes:
    """Build CDC's documented ``request_xml`` document for one query."""

    root = ET.Element("request-parameters")
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for name, value in wonder_parameters(query):
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(value)
    for name in order:
        _add_parameter(root, name, grouped[name])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


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


def _cell_value(cell: ET.Element) -> str:
    for attribute in ("v", "dt", "l"):
        if attribute in cell.attrib:
            return cell.attrib[attribute].strip()
    return "".join(cell.itertext()).strip()


def _is_suppressed(values: Sequence[str]) -> bool:
    combined = " ".join(values).lower()
    return any(marker in combined for marker in ("suppressed", "unreliable", "---"))


def _validate_years(
    observations: Sequence[MortalityObservation], query: MortalityQuery
) -> None:
    years = [observation.year for observation in observations]
    duplicates = sorted({year for year in years if years.count(year) > 1})
    if duplicates:
        raise SourceValidationError(
            f"CDC WONDER returned duplicate/conflicting rows for {query.key}: "
            + ", ".join(map(str, duplicates))
        )
    missing = sorted(set(query.required_years) - set(years))
    if missing:
        raise SourceValidationError(
            f"CDC WONDER is missing required observations for {query.key}: "
            + ", ".join(map(str, missing))
        )


def parse_wonder_xml(
    body: bytes, query: MortalityQuery
) -> tuple[MortalityObservation, ...]:
    """Parse and validate the data table in a CDC WONDER XML response."""

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

    observations: list[MortalityObservation] = []
    for row in table.findall("r"):
        cells = row.findall("c")
        if len(cells) < 4:
            continue
        year_text = _cell_value(cells[0]).strip()
        if not year_text.isdigit():
            continue  # CDC's final total row has no year label.
        year = int(year_text)
        if year not in query.required_years:
            raise SourceValidationError(
                f"CDC WONDER returned out-of-range year {year} for {query.key}"
            )
        values = [_cell_value(cell) for cell in cells[1:4]]
        if _is_suppressed(values) or not values[0]:
            observations.append(
                MortalityObservation(
                    year=year,
                    deaths=None,
                    population=None,
                    crude_rate=None,
                    source_reported_crude_rate=None,
                    icd10_selection=query.icd10_selection,
                    disaggregation={},
                    suppression_status="suppressed",
                    source_notes=(
                        DEFAULT_SOURCE_NOTE,
                        "CDC WONDER suppressed this observation; it is not published.",
                    ),
                )
            )
            continue
        deaths = _parse_int(values[0], "deaths", year)
        population = _parse_int(values[1], "population", year)
        reported_rate = _parse_decimal(values[2], "crude rate", year)
        observations.append(
            MortalityObservation(
                year=year,
                deaths=deaths,
                population=population,
                crude_rate=calculate_crude_rate(deaths, population),
                source_reported_crude_rate=reported_rate,
                icd10_selection=query.icd10_selection,
                disaggregation={},
                suppression_status="not_suppressed",
                source_notes=(DEFAULT_SOURCE_NOTE,),
            )
        )

    _validate_years(observations, query)
    return tuple(sorted(observations, key=lambda observation: observation.year))


def parse_wonder_tsv(
    body: bytes, query: MortalityQuery
) -> tuple[MortalityObservation, ...]:
    """Parse CDC WONDER's official tab-separated download format."""

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
    expected = {"Year Code", "Deaths", "Population", "Crude Rate"}
    missing = sorted(expected - set(header))
    if missing:
        raise SourceValidationError(
            "CDC WONDER TSV is missing columns: " + ", ".join(missing)
        )
    indexes = {name: header.index(name) for name in expected}

    observations: list[MortalityObservation] = []
    for row in reader:
        if not row or row[0].strip() in {"---", "Total", "Caveats:"}:
            break
        padded = row + [""] * (len(header) - len(row))
        year_text = padded[indexes["Year Code"]].strip()
        if not year_text.isdigit():
            continue
        year = int(year_text)
        if year not in query.required_years:
            raise SourceValidationError(
                f"CDC WONDER TSV returned out-of-range year {year} for {query.key}"
            )
        values = [
            padded[indexes["Deaths"]],
            padded[indexes["Population"]],
            padded[indexes["Crude Rate"]],
        ]
        if _is_suppressed(values) or not values[0].strip():
            observations.append(
                MortalityObservation(
                    year=year,
                    deaths=None,
                    population=None,
                    crude_rate=None,
                    source_reported_crude_rate=None,
                    icd10_selection=query.icd10_selection,
                    disaggregation={},
                    suppression_status="suppressed",
                    source_notes=(
                        DEFAULT_SOURCE_NOTE,
                        "CDC WONDER suppressed this observation; it is not published.",
                    ),
                )
            )
            continue
        deaths = _parse_int(values[0], "deaths", year)
        population = _parse_int(values[1], "population", year)
        reported_rate = _parse_decimal(values[2], "crude rate", year)
        observations.append(
            MortalityObservation(
                year=year,
                deaths=deaths,
                population=population,
                crude_rate=calculate_crude_rate(deaths, population),
                source_reported_crude_rate=reported_rate,
                icd10_selection=query.icd10_selection,
                disaggregation={},
                suppression_status="not_suppressed",
                source_notes=(DEFAULT_SOURCE_NOTE,),
            )
        )

    _validate_years(observations, query)
    return tuple(sorted(observations, key=lambda observation: observation.year))


def _validate_batch(
    observations: Mapping[str, tuple[MortalityObservation, ...]],
    queries: Sequence[MortalityQuery],
) -> None:
    expected_keys = {query.key for query in queries}
    if set(observations) != expected_keys:
        raise SourceValidationError(
            "Mortality batch keys do not match requested queries"
        )
    populations: dict[int, int] = {}
    for query in queries:
        rows = observations[query.key]
        _validate_years(rows, query)
        for row in rows:
            if row.population is None:
                continue
            existing = populations.get(row.year)
            if existing is not None and existing != row.population:
                raise SourceValidationError(
                    f"Conflicting CDC WONDER population denominators for {row.year}"
                )
            populations[row.year] = row.population


def fetch_from_wonder_api(
    queries: Sequence[MortalityQuery],
    retrieval_date: str,
    *,
    request_executor: RequestExecutor = request_bytes,
    inter_query_delay_seconds: float = INTER_QUERY_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> NvssMortalityResult:
    """Retrieve each suppression-safe aggregate through the documented XML API."""

    observations: dict[str, tuple[MortalityObservation, ...]] = {}
    for query_number, query in enumerate(queries):
        xml_body = build_wonder_request_xml(query)
        encoded = urllib.parse.urlencode(
            {
                "request_xml": xml_body.decode("utf-8"),
                "accept_datause_restrictions": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            WONDER_QUERY_URL,
            data=encoded,
            headers={
                "Accept": "application/xml,text/xml",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        body, content_type = request_executor(request, WONDER_QUERY_URL)
        # WONDER currently labels valid XML responses as text/html. The parser
        # below verifies the actual document root and rejects real HTML.
        if content_type not in {
            "application/xml",
            "text/xml",
            "text/plain",
            "text/html",
        }:
            raise SourceValidationError(
                f"CDC WONDER API returned unexpected content type {content_type!r}"
            )
        observations[query.key] = parse_wonder_xml(body, query)
        if query_number < len(queries) - 1 and inter_query_delay_seconds:
            sleep(inter_query_delay_seconds)

    _validate_batch(observations, queries)
    return NvssMortalityResult(
        observations=observations,
        source_organization=SOURCE_ORGANIZATION,
        source_dataset=SOURCE_DATASET,
        source_url=WONDER_QUERY_URL,
        retrieval_method="cdc_wonder_api",
        retrieval_date=retrieval_date,
    )


def _session_action(body: bytes) -> str:
    try:
        text = body.decode("utf-8", errors="replace")
    except AttributeError as error:
        raise SourceValidationError("CDC WONDER consent response is invalid") from error
    match = re.search(
        r'action="(/controller/datarequest/D158;jsessionid=[^\"]+)"', text
    )
    if not match:
        raise SourceValidationError(
            "CDC WONDER did not provide a session-aware request form"
        )
    return urllib.parse.urljoin(WONDER_PAGE_URL, html.unescape(match.group(1)))


def fetch_from_wonder_tsv(
    queries: Sequence[MortalityQuery],
    retrieval_date: str,
    *,
    request_executor: RequestExecutor = request_bytes,
    inter_query_delay_seconds: float = INTER_QUERY_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> NvssMortalityResult:
    """Fallback to CDC WONDER's official session-aware TSV downloads."""

    consent_request = urllib.request.Request(
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
    consent_body, consent_type = request_executor(consent_request, WONDER_PAGE_URL)
    if consent_type != "text/html":
        raise SourceValidationError(
            f"CDC WONDER consent returned unexpected content type {consent_type!r}"
        )
    session_url = _session_action(consent_body)

    observations: dict[str, tuple[MortalityObservation, ...]] = {}
    for query_number, query in enumerate(queries):
        form_request = urllib.request.Request(
            session_url,
            data=urllib.parse.urlencode(wonder_parameters(query)).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        result_body, result_type = request_executor(form_request, WONDER_PAGE_URL)
        if result_type != "text/html" or b" Results" not in result_body:
            raise SourceValidationError(
                f"CDC WONDER web query failed for {query.key}"
            )

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
        tsv_body, tsv_type = request_executor(export_request, WONDER_PAGE_URL)
        if tsv_type not in {
            "text/plain",
            "text/tab-separated-values",
            "application/octet-stream",
            # The current export endpoint labels its TSV body as text/html.
            # ``parse_wonder_tsv`` validates the bytes and rejects markup.
            "text/html",
        }:
            raise SourceValidationError(
                f"CDC WONDER export returned unexpected content type {tsv_type!r}"
            )
        observations[query.key] = parse_wonder_tsv(tsv_body, query)
        if query_number < len(queries) - 1 and inter_query_delay_seconds:
            sleep(inter_query_delay_seconds)

    _validate_batch(observations, queries)
    return NvssMortalityResult(
        observations=observations,
        source_organization=SOURCE_ORGANIZATION,
        source_dataset=SOURCE_DATASET,
        source_url=WONDER_PAGE_URL,
        retrieval_method="cdc_wonder_tsv_fallback",
        retrieval_date=retrieval_date,
        source_warnings=(
            "The XML API was unavailable; the official CDC WONDER TSV export "
            "was used instead.",
        ),
    )


def fetch_mortality_batch(
    queries: Sequence[MortalityQuery],
    *,
    retrieval_date: str | None = None,
    api_fetcher: BatchFetcher | None = None,
    fallback_fetcher: BatchFetcher | None = None,
) -> NvssMortalityResult:
    """Retrieve a coordinated batch, falling back only after API failure.

    Every query is an aggregate at the requested ICD selection.  This avoids
    summing individually suppressed ICD cells, which could undercount deaths.
    """

    if not queries:
        raise ValueError("At least one mortality query is required")
    keys = [query.key for query in queries]
    if len(keys) != len(set(keys)):
        raise ValueError("Mortality query keys must be unique")
    run_date = retrieval_date or current_retrieval_date()
    primary = api_fetcher or (
        lambda requested, date: fetch_from_wonder_api(requested, date)
    )
    fallback = fallback_fetcher or (
        lambda requested, date: fetch_from_wonder_tsv(requested, date)
    )

    try:
        result = primary(queries, run_date)
    except RetrievalError as api_error:
        try:
            result = fallback(queries, run_date)
        except RetrievalError as fallback_error:
            raise RetrievalError(
                "CDC WONDER API and official TSV fallback both failed: "
                f"API: {api_error}; fallback: {fallback_error}"
            ) from fallback_error

    _validate_batch(result.observations, queries)
    return result
