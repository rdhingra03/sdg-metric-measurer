"""Reusable retrieval and parsing for Census CPS supplement microdata."""

from __future__ import annotations

import gzip
import io
import json
import os
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Iterator, Mapping, Sequence, Tuple

from ..errors import RetrievalError, SourceValidationError
from ..http import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    PROJECT_USER_AGENT,
    request_bytes as shared_request_bytes,
)
from ..output import current_retrieval_date


API_FIRST_YEAR = 2000
API_LAST_YEAR = 2024
WEIGHT_SCALE = 10_000
HTTP_TIMEOUT_SECONDS = DEFAULT_HTTP_TIMEOUT_SECONDS
USER_AGENT = f"{PROJECT_USER_AGENT} (official Census public data client)"
CENSUS_SOURCE_ORGANIZATION = (
    "U.S. Census Bureau / National Center for Education Statistics"
)
CENSUS_SOURCE_DATASET = "Current Population Survey, October School Enrollment Supplement"
DEFAULT_API_DATASET_PATH = "cps/school/oct"

RequestExecutor = Callable[[urllib.request.Request, str], Tuple[bytes, str]]
WarningHandler = Callable[[str], None]


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
    """Named field positions verified against CPS technical documentation."""

    name: str
    minimum_record_length: int
    fields: Mapping[str, FieldPosition]


@dataclass(frozen=True)
class DownloadConfig:
    """Official fallback file and parsing instructions for one survey year."""

    url: str
    file_format: str
    layout: FixedWidthLayout
    archive_member: str | None = None


@dataclass(frozen=True)
class CpsObservation:
    """One CPS person record containing the variables requested by a caller."""

    values: Mapping[str, int]

    def value(self, variable: str) -> int:
        """Return one requested variable by its published CPS name."""

        return self.values[variable]


@dataclass(frozen=True)
class CpsResult:
    """CPS observations together with source and retrieval provenance."""

    observations: Sequence[object]
    source_organization: str
    source_dataset: str
    source_url: str
    retrieval_method: str
    retrieval_date: str
    source_warnings: tuple[str, ...]
    weight_variable: str
    weight_scale: int


def census_download_url(year: int) -> str:
    """Return the official Census ZIP URL for a recent October CPS file."""

    short_year = str(year)[-2:]
    return (
        "https://www2.census.gov/programs-surveys/cps/datasets/"
        f"{year}/supp/oct{short_year}pub.zip"
    )


LAYOUT_2018_2024 = FixedWidthLayout(
    name="CPS October School Enrollment 2018-2024",
    minimum_record_length=1090,
    fields={
        "PRTAGE": FieldPosition(122, 123),
        "PESEX": FieldPosition(129, 130),
        "PESCH35": FieldPosition(1027, 1028),
        "PECHGRDE": FieldPosition(1033, 1034),
        "PWSUPWGT": FieldPosition(1081, 1090),
    },
)

DOWNLOAD_CONFIGS: Dict[int, DownloadConfig] = {
    year: DownloadConfig(
        url=census_download_url(year),
        file_format="zip",
        layout=LAYOUT_2018_2024,
        archive_member=f"oct{str(year)[-2:]}pub.dat",
    )
    for year in range(2018, 2025)
}
DOWNLOAD_CONFIGS[2019] = DownloadConfig(
    url=census_download_url(2019),
    file_format="zip",
    layout=LAYOUT_2018_2024,
    archive_member="cpspb/supp/data/oct19/oct19pub.dat",
)


def configured_api_key(environment: Mapping[str, str] | None = None) -> str | None:
    """Read the optional Census key without ever placing it in provenance."""

    environment = os.environ if environment is None else environment
    return environment.get("CENSUS_API_KEY", "").strip() or None


def weight_variable_for_year(year: int) -> str:
    """Return the historically correct CPS person weight variable."""

    return "PWSSWGT" if year <= 2005 else "PWSUPWGT"


def api_dataset_url(
    year: int, dataset_path: str = DEFAULT_API_DATASET_PATH
) -> str:
    """Return the public, credential-free landing page for an API dataset."""

    return f"https://api.census.gov/data/{year}/{dataset_path}.html"


def request_bytes(
    request: urllib.request.Request, display_url: str
) -> Tuple[bytes, str]:
    """Execute a Census request without exposing a key in error messages."""

    return shared_request_bytes(
        request, display_url=display_url, timeout=HTTP_TIMEOUT_SECONDS
    )


def parse_integer(value: object, variable: str, year: int) -> int:
    """Parse a required CPS integer with a useful source-validation error."""

    text = str(value).strip()
    try:
        return int(text)
    except ValueError as error:
        raise SourceValidationError(
            f"Invalid {variable} value in {year} CPS data: {text!r}"
        ) from error


def validate_required_variables(
    required_variables: Sequence[str], available_variables: Iterable[str], context: str
) -> None:
    """Fail clearly when a response or layout lacks requested variables."""

    available = set(available_variables)
    missing = [variable for variable in required_variables if variable not in available]
    if missing:
        raise SourceValidationError(
            f"{context} omitted required variables: " + ", ".join(missing)
        )


def observation_from_mapping(
    row: Mapping[str, object], year: int, required_variables: Sequence[str]
) -> CpsObservation:
    """Convert named CPS fields to one validated person observation."""

    validate_required_variables(required_variables, row, f"CPS data for {year}")
    return CpsObservation(
        {
            variable: parse_integer(row[variable], variable, year)
            for variable in required_variables
        }
    )


def fetch_from_api(
    year: int,
    api_key: str,
    required_variables: Sequence[str],
    *,
    dataset_path: str = DEFAULT_API_DATASET_PATH,
    query_filters: Mapping[str, object] | None = None,
    request_executor: RequestExecutor = request_bytes,
) -> list[CpsObservation]:
    """Retrieve named CPS person variables from the Census Microdata API."""

    if not (API_FIRST_YEAR <= year <= API_LAST_YEAR):
        raise RetrievalError(f"The configured Census API does not support {year}")
    if not required_variables:
        raise ValueError("required_variables cannot be empty")

    base_url = f"https://api.census.gov/data/{year}/{dataset_path}"
    parameters = {"get": ",".join(required_variables), "key": api_key}
    parameters.update(
        {name: str(value) for name, value in (query_filters or {}).items()}
    )
    query = urllib.parse.urlencode(parameters)
    request = urllib.request.Request(
        f"{base_url}?{query}",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    body, content_type = request_executor(
        request, api_dataset_url(year, dataset_path)
    )

    if body.lstrip().startswith(b"<") or content_type not in {
        "application/json",
        "text/json",
        "text/plain",
    }:
        preview = body.lstrip()[:80].decode("utf-8", errors="replace")
        raise SourceValidationError(
            f"Census API returned a non-JSON response for {year} "
            f"({content_type!r}; starts with {preview!r})"
        )
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SourceValidationError(
            f"Census API returned invalid JSON for {year}"
        ) from error

    if not isinstance(payload, list) or len(payload) < 2:
        raise SourceValidationError(
            f"Census API returned no person records for {year}"
        )
    header = payload[0]
    if not isinstance(header, list):
        raise SourceValidationError(f"Census API returned a malformed header for {year}")
    validate_required_variables(
        required_variables, header, f"Census API response for {year}"
    )

    observations = []
    for values in payload[1:]:
        if not isinstance(values, list) or len(values) != len(header):
            raise SourceValidationError(
                f"Census API returned a malformed row for {year}"
            )
        observations.append(
            observation_from_mapping(
                dict(zip(header, values)), year, required_variables
            )
        )
    return observations


def open_downloaded_records(
    body: bytes, config: DownloadConfig, year: int
) -> Iterator[bytes]:
    """Yield raw records from a validated ZIP or gzip response in memory."""

    if config.file_format == "zip":
        if not body.startswith(b"PK"):
            raise SourceValidationError(
                f"Census download for {year} is not a ZIP file"
            )
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                member = config.archive_member
                if member is None or member not in archive.namelist():
                    raise SourceValidationError(
                        f"Census ZIP for {year} does not contain {member!r}"
                    )
                with archive.open(member) as stream:
                    yield from stream
        except zipfile.BadZipFile as error:
            raise SourceValidationError(
                f"Census ZIP for {year} is corrupt"
            ) from error
        return

    if config.file_format == "gzip":
        if not body.startswith(b"\x1f\x8b"):
            raise SourceValidationError(
                f"Census download for {year} is not gzip data"
            )
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                yield from stream
        except OSError as error:
            raise SourceValidationError(
                f"Census gzip file for {year} is corrupt"
            ) from error
        return

    raise SourceValidationError(
        f"Unsupported configured file format for {year}: {config.file_format}"
    )


def parse_fixed_width_record(
    raw_line: bytes,
    layout: FixedWidthLayout,
    year: int,
    required_variables: Sequence[str],
    *,
    record_filters: Mapping[str, int] | None = None,
) -> CpsObservation | None:
    """Parse requested variables using one verified year/layout mapping."""

    variables_to_read = list(
        dict.fromkeys([*required_variables, *(record_filters or {}).keys()])
    )
    validate_required_variables(
        variables_to_read, layout.fields, f"Fixed-width layout {layout.name!r}"
    )
    record = raw_line.rstrip(b"\r\n")
    if not record:
        return None
    if len(record) < layout.minimum_record_length:
        raise SourceValidationError(
            f"Short fixed-width record in {year}: expected at least "
            f"{layout.minimum_record_length} bytes, found {len(record)}"
        )

    parsed = {
        variable: parse_integer(layout.fields[variable].read(record), variable, year)
        for variable in variables_to_read
    }
    if any(
        parsed[variable] != expected
        for variable, expected in (record_filters or {}).items()
    ):
        return None
    return CpsObservation(
        {variable: parsed[variable] for variable in required_variables}
    )


def fetch_from_download(
    year: int,
    required_variables: Sequence[str],
    *,
    download_configs: Mapping[int, DownloadConfig] = DOWNLOAD_CONFIGS,
    record_filters: Mapping[str, int] | None = None,
    request_executor: RequestExecutor = request_bytes,
    records_description: str = "matching person records",
) -> tuple[list[CpsObservation], str]:
    """Download and parse one configured official CPS public-use file."""

    config = download_configs.get(year)
    if config is None:
        raise RetrievalError(
            f"No verified downloadable-file layout is configured for {year}. "
            "Use a supported API year with CENSUS_API_KEY or add a layout "
            "from that year's official technical documentation."
        )
    validate_required_variables(
        [*required_variables, *(record_filters or {}).keys()],
        config.layout.fields,
        f"Fixed-width layout {config.layout.name!r}",
    )

    request = urllib.request.Request(
        config.url, headers={"User-Agent": USER_AGENT}
    )
    body, _content_type = request_executor(request, config.url)
    observations = []
    for raw_line in open_downloaded_records(body, config, year):
        observation = parse_fixed_width_record(
            raw_line,
            config.layout,
            year,
            required_variables,
            record_filters=record_filters,
        )
        if observation is not None:
            observations.append(observation)
    if not observations:
        raise SourceValidationError(
            f"No {records_description} were found in the {year} file"
        )
    return observations, config.url


def retrieve_year(
    year: int,
    required_variables: Sequence[str],
    *,
    api_key: str | None = None,
    dataset_path: str = DEFAULT_API_DATASET_PATH,
    source_dataset: str = CENSUS_SOURCE_DATASET,
    query_filters: Mapping[str, object] | None = None,
    record_filters: Mapping[str, int] | None = None,
    download_configs: Mapping[int, DownloadConfig] = DOWNLOAD_CONFIGS,
    api_fetcher: Callable[[], Sequence[object]] | None = None,
    download_fetcher: Callable[[], tuple[Sequence[object], str]] | None = None,
    warning_handler: WarningHandler | None = None,
    retrieval_date: str | None = None,
) -> CpsResult:
    """Prefer a configured Census API and fall back to public-use downloads."""

    weight_variable = weight_variable_for_year(year)
    warnings = []
    api_fetcher = api_fetcher or (
        lambda: fetch_from_api(
            year,
            api_key or "",
            required_variables,
            dataset_path=dataset_path,
            query_filters=query_filters,
        )
    )
    download_fetcher = download_fetcher or (
        lambda: fetch_from_download(
            year,
            required_variables,
            download_configs=download_configs,
            record_filters=record_filters,
        )
    )

    if api_key and API_FIRST_YEAR <= year <= API_LAST_YEAR:
        try:
            observations = api_fetcher()
            method = "api"
            source_url = api_dataset_url(year, dataset_path)
        except RetrievalError as api_error:
            warning = (
                f"{year}: Census API unavailable or invalid: {api_error}\n"
                "  Trying the official downloadable microdata fallback..."
            )
            warnings.append(warning)
            if warning_handler is not None:
                warning_handler(warning)
            observations, source_url = download_fetcher()
            method = "download"
    else:
        if not api_key:
            warning = (
                f"{year}: CENSUS_API_KEY is not configured; using the official "
                "downloadable microdata fallback."
            )
            warnings.append(warning)
            if warning_handler is not None:
                warning_handler(warning)
        observations, source_url = download_fetcher()
        method = "download"

    return CpsResult(
        observations=observations,
        source_organization=CENSUS_SOURCE_ORGANIZATION,
        source_dataset=source_dataset,
        source_url=source_url,
        retrieval_method=method,
        retrieval_date=retrieval_date or current_retrieval_date(),
        source_warnings=tuple(warnings),
        weight_variable=weight_variable,
        weight_scale=WEIGHT_SCALE,
    )
