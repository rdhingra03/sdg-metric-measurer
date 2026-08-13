"""Generic retrieval and validation for OECD SDMX CSV dataflows.

This connector understands the common mechanics of OECD's SDMX service.  It
does not know what an SDG indicator means or how observations should be added
together; those decisions belong in an indicator module.
"""

from __future__ import annotations

import csv
import io
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Tuple

from ..errors import SourceValidationError
from ..http import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    PROJECT_USER_AGENT,
    request_bytes as shared_request_bytes,
)
from ..output import current_retrieval_date


OECD_SDMX_DATA_ENDPOINT = "https://sdmx.oecd.org/dcd-public/rest/data"
OECD_SOURCE_ORGANIZATION = (
    "Organisation for Economic Co-operation and Development (OECD)"
)
HTTP_TIMEOUT_SECONDS = DEFAULT_HTTP_TIMEOUT_SECONDS
USER_AGENT = f"{PROJECT_USER_AGENT} (official OECD SDMX client)"

RequestExecutor = Callable[[urllib.request.Request, str], Tuple[bytes, str]]


@dataclass(frozen=True)
class SdmxQuery:
    """Description of one narrowly filtered OECD SDMX CSV request."""

    agency_id: str
    dataflow_id: str
    version: str
    key: str
    start_year: int
    end_year: int
    source_dataset: str
    dimension_columns: tuple[str, ...]
    expected_dimensions: Mapping[str, frozenset[str]]
    required_years: tuple[int, ...]
    endpoint: str = OECD_SDMX_DATA_ENDPOINT

    @property
    def dataflow_reference(self) -> str:
        """Return the agency, dataflow, and version used by OECD's URL."""

        return f"{self.agency_id},{self.dataflow_id},{self.version}"

    def source_url(self) -> str:
        """Build the exact, reproducible CSV URL for this query."""

        if self.start_year > self.end_year:
            raise ValueError("start_year cannot be later than end_year")
        reference = urllib.parse.quote(self.dataflow_reference, safe=",@._-")
        # Encode ``+`` so it remains SDMX's multi-value operator rather than
        # being mistaken for a space by an intermediary.
        key = urllib.parse.quote(self.key, safe="._-")
        parameters = urllib.parse.urlencode(
            {
                "startPeriod": self.start_year,
                "endPeriod": self.end_year,
                "dimensionAtObservation": "AllDimensions",
                "format": "csvfilewithlabels",
            }
        )
        return f"{self.endpoint}/{reference}/{key}?{parameters}"


@dataclass(frozen=True)
class SdmxObservation:
    """One parsed OECD observation with its identifying dimension codes."""

    year: int
    value: Decimal
    dimensions: Mapping[str, str]

    def dimension(self, name: str) -> str:
        """Return one dimension code validated by the connector."""

        return self.dimensions[name]


@dataclass(frozen=True)
class OecdResult:
    """Validated observations and provenance for one successful SDMX query."""

    observations: tuple[SdmxObservation, ...]
    source_organization: str
    source_dataset: str
    source_url: str
    retrieval_method: str
    retrieval_date: str
    dataflow_id: str
    dataflow_version: str
    warnings: tuple[str, ...] = ()


def request_bytes(
    request: urllib.request.Request, display_url: str
) -> Tuple[bytes, str]:
    """Execute an OECD request through the shared retry-safe HTTP helper."""

    return shared_request_bytes(
        request,
        display_url=display_url,
        timeout=HTTP_TIMEOUT_SECONDS,
    )


def _parse_year(value: object) -> int:
    """Parse an annual SDMX time period and reject non-annual values."""

    text = str(value).strip()
    if len(text) != 4 or not text.isdigit():
        raise SourceValidationError(f"Invalid OECD annual time period: {value!r}")
    return int(text)


def _parse_value(value: object, year: int) -> Decimal:
    """Parse one finite numeric observation without losing precision."""

    text = str(value).strip()
    if not text:
        raise SourceValidationError(f"OECD observation for {year} has no value")
    try:
        number = Decimal(text)
    except InvalidOperation as error:
        raise SourceValidationError(
            f"Invalid OECD numeric observation for {year}: {value!r}"
        ) from error
    if not number.is_finite():
        raise SourceValidationError(
            f"OECD observation for {year} is not a finite number"
        )
    return number


def parse_sdmx_csv(
    body: bytes,
    query: SdmxQuery,
) -> tuple[tuple[SdmxObservation, ...], tuple[str, ...]]:
    """Parse and validate a filtered OECD SDMX CSV response."""

    if body.lstrip().startswith(b"<"):
        raise SourceValidationError("OECD SDMX returned HTML or XML instead of CSV")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SourceValidationError("OECD SDMX CSV is not valid UTF-8") from error

    reader = csv.DictReader(io.StringIO(text, newline=""))
    required_columns = {
        "TIME_PERIOD",
        "OBS_VALUE",
        *query.dimension_columns,
        *query.expected_dimensions,
    }
    missing_columns = sorted(required_columns - set(reader.fieldnames or ()))
    if missing_columns:
        raise SourceValidationError(
            "OECD SDMX CSV is missing required columns: "
            + ", ".join(missing_columns)
        )

    by_key: dict[tuple[int, tuple[tuple[str, str], ...]], SdmxObservation] = {}
    warnings: list[str] = []
    for row_number, row in enumerate(reader, start=2):
        year = _parse_year(row["TIME_PERIOD"])
        if not query.start_year <= year <= query.end_year:
            raise SourceValidationError(
                f"OECD row {row_number} contains out-of-range year {year}"
            )

        for dimension, allowed_values in query.expected_dimensions.items():
            actual = (row.get(dimension) or "").strip()
            if actual not in allowed_values:
                expected = ", ".join(sorted(allowed_values))
                raise SourceValidationError(
                    f"OECD row {row_number} has {dimension}={actual!r}; "
                    f"expected one of: {expected}"
                )

        dimensions = {
            column: (row.get(column) or "").strip()
            for column in query.dimension_columns
        }
        observation = SdmxObservation(
            year=year,
            value=_parse_value(row["OBS_VALUE"], year),
            dimensions=dimensions,
        )
        identity = (
            year,
            tuple((name, dimensions[name]) for name in query.dimension_columns),
        )
        existing = by_key.get(identity)
        if existing is not None:
            if existing.value != observation.value:
                raise SourceValidationError(
                    "OECD SDMX returned conflicting duplicate observations for "
                    f"{year}: {existing.value} and {observation.value}"
                )
            warning = f"Ignored identical duplicate OECD observation for {year}"
            if warning not in warnings:
                warnings.append(warning)
            continue
        by_key[identity] = observation

    if not by_key:
        raise SourceValidationError("OECD SDMX CSV contains no observations")

    available_years = {year for year, _dimensions in by_key}
    missing_years = sorted(set(query.required_years) - available_years)
    if missing_years:
        raise SourceValidationError(
            "OECD SDMX is missing required annual observations: "
            + ", ".join(map(str, missing_years))
        )

    observations = tuple(
        sorted(
            by_key.values(),
            key=lambda observation: (
                observation.year,
                tuple(
                    observation.dimensions[name]
                    for name in query.dimension_columns
                ),
            ),
        )
    )
    return observations, tuple(warnings)


def fetch_sdmx_csv(
    query: SdmxQuery,
    *,
    request_executor: RequestExecutor = request_bytes,
    retrieval_date: str | None = None,
) -> OecdResult:
    """Retrieve and validate one OECD SDMX CSV query with provenance."""

    source_url = query.source_url()
    request = urllib.request.Request(
        source_url,
        headers={
            "Accept": "text/csv",
            "User-Agent": USER_AGENT,
        },
    )
    body, content_type = request_executor(request, source_url)
    if content_type not in {
        "text/csv",
        "text/plain",
        "application/octet-stream",
        "application/vnd.sdmx.data+csv",
    }:
        raise SourceValidationError(
            f"OECD SDMX returned unexpected content type {content_type!r}"
        )

    observations, warnings = parse_sdmx_csv(body, query)
    return OecdResult(
        observations=observations,
        source_organization=OECD_SOURCE_ORGANIZATION,
        source_dataset=query.source_dataset,
        source_url=source_url,
        retrieval_method="api",
        retrieval_date=retrieval_date or current_retrieval_date(),
        dataflow_id=query.dataflow_id,
        dataflow_version=query.version,
        warnings=warnings,
    )
