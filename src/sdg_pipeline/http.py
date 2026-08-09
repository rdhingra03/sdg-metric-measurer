"""Shared, source-neutral HTTP request execution."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Tuple

from .errors import RetrievalError


PROJECT_USER_AGENT = "sdg-metric-measurer/1.0"
DEFAULT_HTTP_TIMEOUT_SECONDS = 180


def request_bytes(
    request: urllib.request.Request,
    display_url: str,
    timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> Tuple[bytes, str]:
    """Return a response body and content type with safe error reporting.

    ``display_url`` must not contain credentials. It is deliberately separate
    from ``request.full_url`` so an API key in the real request cannot appear
    in an exception or terminal report.
    """

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        raise RetrievalError(
            f"Request failed for {display_url}: "
            f"HTTP Error {error.code}: {error.reason}"
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise RetrievalError(f"Request failed for {display_url}: {reason}") from error

