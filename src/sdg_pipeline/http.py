"""Shared, source-neutral HTTP request execution."""

from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Tuple

from .errors import RetrievalError


PROJECT_USER_AGENT = "sdg-metric-measurer/1.0"
DEFAULT_HTTP_TIMEOUT_SECONDS = 180
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 0.25
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504, 520}


def _safe_display_url(display_url: str) -> str:
    """Remove query strings and user credentials from an error-reporting URL."""

    parsed = urllib.parse.urlsplit(display_url)
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, hostname, parsed.path, "", "")
    )


def _redact_request_secrets(
    message: str, request: urllib.request.Request, safe_url: str
) -> str:
    """Remove the real request URL and its credential-like values from text."""

    request_url = request.full_url
    redacted = message.replace(request_url, safe_url)
    parsed = urllib.parse.urlsplit(request_url)
    secret_values = [parsed.username or "", parsed.password or ""]
    secret_values.extend(value for _name, value in urllib.parse.parse_qsl(parsed.query))
    for secret in sorted(set(secret_values), key=len, reverse=True):
        if not secret:
            continue
        for representation in {
            secret,
            urllib.parse.quote(secret, safe=""),
            urllib.parse.quote_plus(secret),
        }:
            if representation:
                redacted = redacted.replace(representation, "[REDACTED]")
    return redacted


def _is_retryable_network_error(error: BaseException) -> bool:
    """Return whether a non-HTTP exception represents a temporary connection issue."""

    reason = getattr(error, "reason", error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(
        reason,
        (
            TimeoutError,
            ConnectionError,
            OSError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ),
    ):
        return True
    reason_text = str(reason).lower()
    return any(
        marker in reason_text
        for marker in (
            "timed out",
            "temporary",
            "connection reset",
            "connection aborted",
            "connection refused",
            "remote end closed",
            "incomplete",
        )
    )


def _safe_error_detail(
    error: BaseException, request: urllib.request.Request, safe_url: str
) -> str:
    """Describe a failure without exposing the request's query credentials."""

    if isinstance(error, urllib.error.HTTPError):
        detail = f"HTTP Error {error.code}: {error.reason}"
    elif isinstance(error, http.client.IncompleteRead):
        detail = (
            "Incomplete HTTP response "
            f"({len(error.partial)} bytes read; {error.expected} more expected)"
        )
    else:
        detail = str(getattr(error, "reason", error))
    return _redact_request_secrets(detail, request, safe_url)


def request_bytes(
    request: urllib.request.Request,
    display_url: str,
    timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    open_request: Callable[..., object] | None = None,
) -> Tuple[bytes, str]:
    """Return an HTTP body, retrying only clearly temporary failures.

    ``display_url`` must not contain credentials. It is deliberately separate
    from ``request.full_url`` so an API key in the real request cannot appear
    in an exception or terminal report.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds cannot be negative")

    safe_url = _safe_display_url(display_url)
    executor = open_request or urllib.request.urlopen
    for attempt in range(1, max_attempts + 1):
        last_error: BaseException
        try:
            with executor(request, timeout=timeout) as response:
                return response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as caught_error:
            last_error = caught_error
            retryable = caught_error.code in RETRYABLE_HTTP_STATUS_CODES
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ) as caught_error:
            last_error = caught_error
            retryable = _is_retryable_network_error(caught_error)

        if not retryable or attempt == max_attempts:
            detail = _safe_error_detail(last_error, request, safe_url)
            attempt_note = (
                f" after {attempt} attempts" if attempt > 1 else ""
            )
            raise RetrievalError(
                f"Request failed for {safe_url}{attempt_note}: {detail}"
            ) from last_error

        sleep(retry_delay_seconds * (2 ** (attempt - 1)))

    raise AssertionError("HTTP retry loop ended unexpectedly")
