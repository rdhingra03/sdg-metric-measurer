"""Generic access to the official UNSD SDG Global Database API.

The connector knows how to retrieve and validate UNSD observations.  It does
not decide which statistical series represents an indicator on a project data
card; those reviewed choices live in :mod:`sdg_pipeline.unsd_comparison`.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Sequence, Tuple

from ..errors import SourceValidationError
from ..http import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    PROJECT_USER_AGENT,
    request_bytes as shared_request_bytes,
)
from ..output import current_retrieval_date


UNSD_API_ROOT = "https://unstats.un.org/SDGAPI/v1/sdg"
INDICATOR_DATA_ENDPOINT = f"{UNSD_API_ROOT}/Indicator/Data"
INDICATOR_LIST_ENDPOINT = f"{UNSD_API_ROOT}/Indicator/List"
SERIES_LAST_UPDATED_ENDPOINT = f"{UNSD_API_ROOT}/Series/LastUpdated"
SERIES_DATA_ENDPOINT = f"{UNSD_API_ROOT}/Series/Data"
UNITED_STATES_M49_CODE = "840"
UNSD_SOURCE_ORGANIZATION = "United Nations Statistics Division (UNSD)"
HTTP_TIMEOUT_SECONDS = DEFAULT_HTTP_TIMEOUT_SECONDS
USER_AGENT = f"{PROJECT_USER_AGENT} (official UNSD SDG API client)"

RequestExecutor = Callable[[urllib.request.Request, str], Tuple[bytes, str]]


@dataclass(frozen=True)
class UnsdObservation:
    """One validated observation returned by the Global SDG Database."""

    indicator_ids: tuple[str, ...]
    series_code: str
    series_description: str
    geography_code: str
    geography_name: str
    year: int
    value: Decimal
    dimensions: Mapping[str, str]
    attributes: Mapping[str, str]
    source: str
    footnotes: tuple[str, ...]
    value_type: str = ""
    lower_bound: str = ""
    upper_bound: str = ""


@dataclass(frozen=True)
class UnsdResult:
    """Validated observations plus retrieval and release provenance."""

    observations: tuple[UnsdObservation, ...]
    raw_observation_count: int
    deduplicated_observation_count: int
    source_organization: str
    source_url: str
    retrieval_method: str
    retrieval_date: str
    database_release: str
    database_last_updated: str
    attribute_descriptions: Mapping[str, Mapping[str, str]]
    dimension_descriptions: Mapping[str, Mapping[str, str]]
    warnings: tuple[str, ...] = ()

    def attribute_description(self, attribute: str, code: str) -> str:
        """Return a human-readable attribute label when UNSD supplied one."""

        return self.attribute_descriptions.get(attribute, {}).get(code, "")


def request_bytes(
    request: urllib.request.Request, display_url: str
) -> Tuple[bytes, str]:
    """Execute a UNSD request through the shared retry-safe HTTP helper."""

    return shared_request_bytes(
        request,
        display_url=display_url,
        timeout=HTTP_TIMEOUT_SECONDS,
    )


def _request_json(
    url: str,
    *,
    request_executor: RequestExecutor,
) -> object:
    """Retrieve one JSON response and reject HTML or malformed payloads."""

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    body, content_type = request_executor(request, url)
    if content_type not in {
        "application/json",
        "text/json",
        "text/plain",
        "application/octet-stream",
    }:
        raise SourceValidationError(
            f"UNSD SDG API returned unexpected content type {content_type!r}"
        )
    if body.lstrip().startswith(b"<"):
        raise SourceValidationError("UNSD SDG API returned HTML or XML instead of JSON")
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceValidationError("UNSD SDG API returned invalid JSON") from error


def _parse_code_list(value: object, field_name: str) -> tuple[str, ...]:
    """Normalize an API code or list of codes into a non-empty tuple."""

    if isinstance(value, str):
        codes = (value.strip(),)
    elif isinstance(value, list):
        codes = tuple(str(item).strip() for item in value)
    else:
        raise SourceValidationError(f"UNSD observation has invalid {field_name}")
    if not codes or any(not code for code in codes):
        raise SourceValidationError(f"UNSD observation has empty {field_name}")
    return codes


def _parse_year(value: object) -> int:
    """Parse an annual UNSD time period without accepting fractional years."""

    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise SourceValidationError(f"Invalid UNSD annual time period: {value!r}") from error
    if not number.is_finite() or number != number.to_integral_value():
        raise SourceValidationError(f"UNSD time period is not annual: {value!r}")
    year = int(number)
    if year < 1900 or year > 2200:
        raise SourceValidationError(f"UNSD annual time period is out of range: {year}")
    return year


def _parse_value(value: object, year: int) -> Decimal:
    """Parse one finite numeric observation without losing precision."""

    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise SourceValidationError(
            f"Invalid UNSD numeric observation for {year}: {value!r}"
        ) from error
    if not number.is_finite():
        raise SourceValidationError(f"UNSD observation for {year} is not finite")
    return number


def _string_mapping(value: object, field_name: str) -> dict[str, str]:
    """Validate an attributes or dimensions object."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SourceValidationError(f"UNSD observation has invalid {field_name}")
    return {str(name): str(code) for name, code in value.items()}


def _footnotes(value: object) -> tuple[str, ...]:
    """Normalize the API's footnote field while preserving its text."""

    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, list):
        raise SourceValidationError("UNSD observation has invalid footnotes")
    return tuple(str(item) for item in value if str(item))


def parse_observation(item: object, expected_area_code: str) -> UnsdObservation:
    """Parse one observation and enforce the requested geography."""

    if not isinstance(item, Mapping):
        raise SourceValidationError("UNSD SDG API data contains a malformed row")
    indicator_ids = _parse_code_list(item.get("indicator"), "indicator codes")
    series_code = str(item.get("series") or "").strip()
    if not series_code:
        raise SourceValidationError("UNSD observation has no series code")
    geography_code = str(item.get("geoAreaCode") or "").strip()
    if geography_code != expected_area_code:
        raise SourceValidationError(
            f"UNSD observation has geography {geography_code!r}; "
            f"expected {expected_area_code!r}"
        )
    year = _parse_year(item.get("timePeriodStart"))
    return UnsdObservation(
        indicator_ids=indicator_ids,
        series_code=series_code,
        series_description=str(item.get("seriesDescription") or "").strip(),
        geography_code=geography_code,
        geography_name=str(item.get("geoAreaName") or "").strip(),
        year=year,
        value=_parse_value(item.get("value"), year),
        dimensions=_string_mapping(item.get("dimensions"), "dimensions"),
        attributes=_string_mapping(item.get("attributes"), "attributes"),
        source=str(item.get("source") or "").strip(),
        footnotes=_footnotes(item.get("footnotes")),
        value_type=str(item.get("valueType") or "").strip(),
        lower_bound=str(item.get("lowerBound") or "").strip(),
        upper_bound=str(item.get("upperBound") or "").strip(),
    )


def _code_descriptions(value: object, field_name: str) -> dict[str, dict[str, str]]:
    """Parse page-level attribute or dimension code descriptions."""

    if not isinstance(value, list):
        raise SourceValidationError(f"UNSD page has invalid {field_name}")
    descriptions: dict[str, dict[str, str]] = {}
    for definition in value:
        if not isinstance(definition, Mapping):
            raise SourceValidationError(f"UNSD page has malformed {field_name}")
        identifier = str(definition.get("id") or "").strip()
        codes = definition.get("codes")
        if not identifier or not isinstance(codes, list):
            raise SourceValidationError(f"UNSD page has malformed {field_name}")
        parsed_codes: dict[str, str] = {}
        for code in codes:
            if not isinstance(code, Mapping):
                raise SourceValidationError(f"UNSD page has malformed {field_name} codes")
            key = str(code.get("code") or "").strip()
            if key:
                parsed_codes[key] = str(code.get("description") or "").strip()
        descriptions[identifier] = parsed_codes
    return descriptions


def parse_data_page(
    payload: object,
    *,
    expected_page: int,
    expected_area_code: str,
) -> tuple[
    tuple[UnsdObservation, ...],
    int,
    int,
    int,
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    """Validate one paginated Indicator/Data response."""

    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise SourceValidationError("UNSD SDG API response does not contain a data list")
    try:
        page_number = int(payload["pageNumber"])
        total_pages = int(payload["totalPages"])
        total_elements = int(payload["totalElements"])
    except (KeyError, TypeError, ValueError) as error:
        raise SourceValidationError("UNSD response has invalid pagination metadata") from error
    if page_number != expected_page or total_pages < 1 or total_elements < 0:
        raise SourceValidationError("UNSD response has inconsistent pagination metadata")
    observations = tuple(
        parse_observation(item, expected_area_code) for item in payload["data"]
    )
    return (
        observations,
        total_pages,
        total_elements,
        len(payload["data"]),
        _code_descriptions(payload.get("attributes"), "attribute definitions"),
        _code_descriptions(payload.get("dimensions"), "dimension definitions"),
    )


def _merge_descriptions(
    destination: dict[str, dict[str, str]],
    incoming: Mapping[str, Mapping[str, str]],
) -> None:
    """Merge repeated page metadata and reject contradictory labels."""

    for identifier, codes in incoming.items():
        existing_codes = destination.setdefault(identifier, {})
        for code, description in codes.items():
            existing = existing_codes.get(code)
            if existing is not None and existing != description:
                raise SourceValidationError(
                    f"UNSD pages disagree on {identifier} code {code!r}"
                )
            existing_codes[code] = description


def _observation_identity(observation: UnsdObservation) -> tuple[object, ...]:
    """Return the complete identity used only for exact de-duplication."""

    return (
        observation.indicator_ids,
        observation.series_code,
        observation.series_description,
        observation.geography_code,
        observation.geography_name,
        observation.year,
        observation.value,
        tuple(sorted(observation.dimensions.items())),
        tuple(sorted(observation.attributes.items())),
        observation.source,
        observation.footnotes,
        observation.value_type,
        observation.lower_bound,
        observation.upper_bound,
    )


def _indicator_data_url(
    indicator_ids: Sequence[str], area_code: str, page: int, page_size: int
) -> str:
    """Build one deterministic multi-indicator page URL."""

    parameters: list[tuple[str, object]] = [
        ("indicator", indicator_id) for indicator_id in indicator_ids
    ]
    parameters.extend(
        [
            ("areaCode", area_code),
            ("page", page),
            ("pageSize", page_size),
        ]
    )
    return f"{INDICATOR_DATA_ENDPOINT}?{urllib.parse.urlencode(parameters)}"


def series_data_url(series_code: str, area_code: str = UNITED_STATES_M49_CODE) -> str:
    """Return a concise official API URL for one series and geography."""

    parameters = urllib.parse.urlencode(
        [("seriesCode", series_code), ("areaCode", area_code)]
    )
    return f"{SERIES_DATA_ENDPOINT}?{parameters}"


def _database_release(payload: object, requested_indicators: set[str]) -> str:
    """Extract the current release covering every requested indicator."""

    if not isinstance(payload, list):
        raise SourceValidationError("UNSD indicator catalogue is not a list")
    releases: set[str] = set()
    found: set[str] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            raise SourceValidationError("UNSD indicator catalogue contains a malformed row")
        code = str(item.get("code") or "").strip()
        if code not in requested_indicators:
            continue
        found.add(code)
        series = item.get("series")
        if not isinstance(series, list) or not series:
            raise SourceValidationError(f"UNSD catalogue has no series for {code}")
        for entry in series:
            if not isinstance(entry, Mapping):
                raise SourceValidationError(f"UNSD catalogue has malformed series for {code}")
            release = str(entry.get("release") or "").strip()
            if release:
                releases.add(release)
    missing = sorted(requested_indicators - found)
    if missing:
        raise SourceValidationError(
            "UNSD indicator catalogue is missing: " + ", ".join(missing)
        )
    if not releases:
        return ""
    return " | ".join(sorted(releases))


def _last_updated(payload: object) -> str:
    """Validate the API's database update timestamp."""

    if not isinstance(payload, str) or not payload.strip():
        raise SourceValidationError("UNSD last-updated response is invalid")
    return payload.strip()


def fetch_indicator_observations(
    indicator_ids: Sequence[str],
    *,
    area_code: str = UNITED_STATES_M49_CODE,
    page_size: int = 5000,
    request_executor: RequestExecutor = request_bytes,
    retrieval_date: str | None = None,
) -> UnsdResult:
    """Retrieve multiple indicators, following every page and preserving metadata."""

    normalized_ids = tuple(dict.fromkeys(str(value).strip() for value in indicator_ids))
    if not normalized_ids or any(not value for value in normalized_ids):
        raise ValueError("indicator_ids must contain at least one non-empty code")
    if not str(area_code).isdigit():
        raise ValueError("area_code must be an M49 numeric code")
    if page_size < 1:
        raise ValueError("page_size must be positive")

    catalogue = _request_json(
        INDICATOR_LIST_ENDPOINT,
        request_executor=request_executor,
    )
    database_release = _database_release(catalogue, set(normalized_ids))
    database_last_updated = _last_updated(
        _request_json(
            SERIES_LAST_UPDATED_ENDPOINT,
            request_executor=request_executor,
        )
    )

    all_observations: list[UnsdObservation] = []
    raw_count = 0
    total_pages: int | None = None
    total_elements: int | None = None
    attribute_descriptions: dict[str, dict[str, str]] = {}
    dimension_descriptions: dict[str, dict[str, str]] = {}
    source_url = _indicator_data_url(normalized_ids, area_code, 1, page_size)

    page = 1
    while total_pages is None or page <= total_pages:
        url = _indicator_data_url(normalized_ids, area_code, page, page_size)
        payload = _request_json(url, request_executor=request_executor)
        (
            observations,
            page_total,
            page_elements,
            page_raw_count,
            page_attributes,
            page_dimensions,
        ) = parse_data_page(
            payload,
            expected_page=page,
            expected_area_code=area_code,
        )
        if total_pages is None:
            total_pages = page_total
            total_elements = page_elements
        elif total_pages != page_total or total_elements != page_elements:
            raise SourceValidationError("UNSD pagination changed during retrieval")
        all_observations.extend(observations)
        raw_count += page_raw_count
        _merge_descriptions(attribute_descriptions, page_attributes)
        _merge_descriptions(dimension_descriptions, page_dimensions)
        page += 1

    if total_elements is None or raw_count != total_elements:
        raise SourceValidationError(
            f"UNSD reported {total_elements} observations but returned {raw_count}"
        )
    if not all_observations:
        raise SourceValidationError("UNSD returned no observations")

    unique: dict[tuple[object, ...], UnsdObservation] = {}
    duplicate_count = 0
    for observation in all_observations:
        identity = _observation_identity(observation)
        if identity in unique:
            duplicate_count += 1
            continue
        unique[identity] = observation

    observations = tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.series_code,
                item.year,
                tuple(sorted(item.dimensions.items())),
                item.value,
            ),
        )
    )
    warnings = (
        (f"Removed {duplicate_count} exact duplicate UNSD observations",)
        if duplicate_count
        else ()
    )
    return UnsdResult(
        observations=observations,
        raw_observation_count=raw_count,
        deduplicated_observation_count=len(observations),
        source_organization=UNSD_SOURCE_ORGANIZATION,
        source_url=source_url,
        retrieval_method="api",
        retrieval_date=retrieval_date or current_retrieval_date(),
        database_release=database_release,
        database_last_updated=database_last_updated,
        attribute_descriptions=attribute_descriptions,
        dimension_descriptions=dimension_descriptions,
        warnings=warnings,
    )
