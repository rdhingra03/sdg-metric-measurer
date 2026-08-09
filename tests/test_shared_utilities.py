"""Focused tests for the shared Phase 2 infrastructure helpers."""

from __future__ import annotations

import io
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

    def test_error_uses_safe_display_url_not_credentialed_request_url(self) -> None:
        request = urllib.request.Request(
            "https://example.invalid/data?key=super-secret"
        )
        with mock.patch(
            "sdg_pipeline.http.urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            with self.assertRaises(RetrievalError) as context:
                request_bytes(request, "https://example.invalid/data")

        message = str(context.exception)
        self.assertIn("https://example.invalid/data", message)
        self.assertNotIn("super-secret", message)


if __name__ == "__main__":
    unittest.main()
