"""Focused tests for the shared Phase 2 infrastructure helpers."""

from __future__ import annotations

import io
import http.client
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from pipeline_test_utils import SRC_ROOT  # Also makes the src package importable.
from sdg_pipeline.archive import read_nested_zip_member
from sdg_pipeline.errors import RetrievalError
from sdg_pipeline.http import request_bytes


class NestedArchiveTests(unittest.TestCase):
    """Protect read-only access to a ZIP held inside a TAR."""

    def test_reads_requested_member_without_extracting_files(self) -> None:
        inner_bytes = io.BytesIO()
        with zipfile.ZipFile(inner_bytes, mode="w") as inner_archive:
            inner_archive.writestr("fixture/data.csv", "Year,Value\n2022,1\n")

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            tar_path = temporary_path / "fixture.tar"
            payload = inner_bytes.getvalue()
            member = tarfile.TarInfo("SDGs/sdg-master.zip")
            member.size = len(payload)
            with tarfile.open(tar_path, mode="w") as outer_archive:
                outer_archive.addfile(member, io.BytesIO(payload))

            result = read_nested_zip_member(
                tar_path, "SDGs/sdg-master.zip", "fixture/data.csv"
            )

            self.assertEqual(result, b"Year,Value\n2022,1\n")
            self.assertEqual(list(temporary_path.iterdir()), [tar_path])


class HttpHelperTests(unittest.TestCase):
    """Protect safe HTTP error handling, especially credential redaction."""

    class FakeHeaders:
        def get_content_type(self) -> str:
            return "application/json"

    class FakeResponse:
        def __init__(self, body: bytes = b"{}", read_error=None) -> None:
            self.body = body
            self.read_error = read_error
            self.headers = HttpHelperTests.FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, _error_type, _error, _traceback) -> None:
            return None

        def read(self) -> bytes:
            if self.read_error is not None:
                raise self.read_error
            return self.body

    def test_temporary_http_failure_is_retried_then_succeeds(self) -> None:
        request = urllib.request.Request("https://example.invalid/data")
        temporary_error = urllib.error.HTTPError(
            request.full_url, 503, "Service Unavailable", {}, None
        )
        sleeper = mock.Mock()
        with mock.patch(
            "sdg_pipeline.http.urllib.request.urlopen",
            side_effect=[temporary_error, self.FakeResponse(b'{"ok": true}')],
        ) as urlopen:
            body, content_type = request_bytes(
                request,
                request.full_url,
                retry_delay_seconds=0.1,
                sleep=sleeper,
            )

        self.assertEqual(b'{"ok": true}', body)
        self.assertEqual("application/json", content_type)
        self.assertEqual(2, urlopen.call_count)
        sleeper.assert_called_once_with(0.1)

    def test_retryable_failures_exhaust_bounded_attempts(self) -> None:
        request = urllib.request.Request("https://example.invalid/data")
        sleeper = mock.Mock()

        def unavailable(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                request.full_url, 520, "Temporary host failure", {}, None
            )

        with mock.patch(
            "sdg_pipeline.http.urllib.request.urlopen", side_effect=unavailable
        ) as urlopen:
            with self.assertRaisesRegex(RetrievalError, "after 3 attempts"):
                request_bytes(
                    request,
                    request.full_url,
                    max_attempts=3,
                    retry_delay_seconds=0.1,
                    sleep=sleeper,
                )

        self.assertEqual(3, urlopen.call_count)
        self.assertEqual([mock.call(0.1), mock.call(0.2)], sleeper.call_args_list)

    def test_incomplete_response_is_converted_to_retrieval_error(self) -> None:
        request = urllib.request.Request("https://example.invalid/data")
        response = self.FakeResponse(
            read_error=http.client.IncompleteRead(b"partial", 100)
        )
        with mock.patch(
            "sdg_pipeline.http.urllib.request.urlopen", return_value=response
        ):
            with self.assertRaisesRegex(
                RetrievalError, "Incomplete HTTP response"
            ):
                request_bytes(request, request.full_url, max_attempts=1)

    def test_non_retryable_http_error_fails_immediately(self) -> None:
        request = urllib.request.Request("https://example.invalid/missing")
        not_found = urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, None
        )
        sleeper = mock.Mock()
        with mock.patch(
            "sdg_pipeline.http.urllib.request.urlopen", side_effect=not_found
        ) as urlopen:
            with self.assertRaisesRegex(RetrievalError, "HTTP Error 404"):
                request_bytes(request, request.full_url, sleep=sleeper)

        self.assertEqual(1, urlopen.call_count)
        sleeper.assert_not_called()

    def test_error_uses_safe_display_url_not_credentialed_request_url(self) -> None:
        request = urllib.request.Request(
            "https://example.invalid/data?key=super-secret"
        )
        with mock.patch(
            "sdg_pipeline.http.urllib.request.urlopen",
            side_effect=urllib.error.URLError(
                ConnectionResetError(
                    "connection failed for "
                    "https://example.invalid/data?key=super-secret"
                )
            ),
        ):
            with self.assertRaises(RetrievalError) as context:
                request_bytes(
                    request,
                    "https://example.invalid/data?key=super-secret",
                    max_attempts=1,
                )

        message = str(context.exception)
        self.assertIn("https://example.invalid/data", message)
        self.assertNotIn("super-secret", message)


if __name__ == "__main__":
    unittest.main()
