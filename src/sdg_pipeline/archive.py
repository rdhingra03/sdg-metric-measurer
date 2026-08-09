"""Read canonical legacy files from the nested SDG archives without extraction."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path


class ArchiveReadError(RuntimeError):
    """A requested member could not be read from the nested legacy archive."""


def read_nested_zip_member(
    tar_path: Path, zip_member: str, requested_member: str
) -> bytes:
    """Read one member from a ZIP stored inside a TAR archive.

    Both archives are opened read-only. The inner ZIP is held in memory, and
    no source file is modified or permanently extracted.
    """

    try:
        with tarfile.open(tar_path, mode="r:*") as outer_archive:
            member = outer_archive.getmember(zip_member)
            archived_zip = outer_archive.extractfile(member)
            if archived_zip is None:
                raise ArchiveReadError(f"Could not read {zip_member}")
            zip_bytes = io.BytesIO(archived_zip.read())

        with zipfile.ZipFile(zip_bytes) as inner_archive:
            return inner_archive.read(requested_member)
    except ArchiveReadError:
        raise
    except (KeyError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise ArchiveReadError(
            f"Could not read {requested_member} from {zip_member} in {tar_path}"
        ) from error

