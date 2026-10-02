"""Private, streaming storage for CRM timeline comment attachments."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from core.services import intake_storage

MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024
DEFAULT_STORAGE_ROOT = Path("./data/sales/timeline-attachments")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


class AttachmentRejected(ValueError):
    """A source file or persisted timeline attachment failed a safety check."""


@dataclass(frozen=True)
class StoredTimelineFile:
    sha256: str
    size_bytes: int
    storage_path: str


def storage_root() -> Path:
    """Use the Docker-persisted app data volume by default."""
    return DEFAULT_STORAGE_ROOT


def _path_for_digest(sha256: str) -> str:
    if not _SHA256_RE.fullmatch(sha256):
        raise AttachmentRejected("Invalid content digest")
    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}"


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sync_ancestor_chain(path: Path) -> None:
    """Persist directory entries up to the filesystem root, including on retry."""
    directory = path
    while True:
        _sync_directory(directory)
        if directory.parent == directory:
            break
        directory = directory.parent


def _mkdir_durable(path: Path) -> None:
    missing: list[Path] = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        if os.name == "nt":
            directory.mkdir(exist_ok=True)
        else:
            directory.mkdir(mode=0o700, exist_ok=True)
        _sync_directory(directory.parent)


def store_attachment(
    chunks: Iterable[bytes],
    *,
    root: Path | None = None,
    max_size_bytes: int = MAX_ATTACHMENT_BYTES,
) -> StoredTimelineFile:
    """Stream bytes to a SHA-addressed path; install atomically and never overwrite."""
    if not 0 <= max_size_bytes <= MAX_ATTACHMENT_BYTES:
        raise AttachmentRejected("Invalid size limit")
    root = root or storage_root()
    digest = hashlib.sha256()
    size = 0
    temporary: Path | None = None
    try:
        staging_dir = intake_storage.resolve_path(root, ".staging")
        _mkdir_durable(staging_dir)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=staging_dir, prefix=".timeline-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            for chunk in chunks:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise AttachmentRejected("Attachment chunks must be bytes")
                data = bytes(chunk)
                size += len(data)
                if size > max_size_bytes:
                    raise AttachmentRejected("Attachment exceeds the size limit")
                digest.update(data)
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        sha256 = digest.hexdigest()
        storage_path = _path_for_digest(sha256)
        destination = intake_storage.resolve_path(root, storage_path)
        _mkdir_durable(destination.parent)
        try:
            # Same-directory hard-link is the atomic create-if-absent operation.
            os.link(temporary, destination)
            temporary.unlink()
        except FileExistsError:
            _verify_path(root, storage_path, sha256, size)
            temporary.unlink()
        # Flush the full chains even when a prior failed attempt created the
        # directories but could not persist their entries.
        _sync_ancestor_chain(destination.parent)
        _sync_ancestor_chain(staging_dir)
        return StoredTimelineFile(sha256, size, storage_path)
    except AttachmentRejected:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise AttachmentRejected("Timeline attachment could not be stored safely") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _verify_path(root: Path, storage_path: str, sha256: str, size_bytes: int) -> Path:
    try:
        path = intake_storage.resolve_path(root, storage_path)
        intake_storage.verify_file(
            root,
            {"storage_path": storage_path, "sha256": sha256, "size_bytes": size_bytes},
        )
        return path
    except (OSError, ValueError, KeyError) as exc:
        raise AttachmentRejected("Stored timeline attachment failed verification") from exc


def verify_attachment(item, *, root: Path | None = None) -> Path:
    """Validate DB metadata and verify the full file before returning a download path."""
    sha256 = item.sha256
    storage_path = item.storage_path
    size_bytes = item.size_bytes
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise AttachmentRejected("Invalid content digest")
    if not isinstance(size_bytes, int) or not 0 <= size_bytes <= MAX_ATTACHMENT_BYTES:
        raise AttachmentRejected("Invalid attachment size")
    if storage_path != _path_for_digest(sha256):
        raise AttachmentRejected("Invalid content-addressed path")
    return _verify_path(root or storage_root(), storage_path, sha256, size_bytes)


def snapshot_attachment(item, *, root: Path | None = None):
    """Copy and verify attachment bytes into a private temporary file for streaming.

    The returned handle is positioned at byte zero. Callers must close it after
    the response finishes, fails, or is cancelled.
    """
    sha256 = item.sha256
    storage_path = item.storage_path
    size_bytes = item.size_bytes
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise AttachmentRejected("Invalid content digest")
    if not isinstance(size_bytes, int) or not 0 <= size_bytes <= MAX_ATTACHMENT_BYTES:
        raise AttachmentRejected("Invalid attachment size")
    if storage_path != _path_for_digest(sha256):
        raise AttachmentRejected("Invalid content-addressed path")

    snapshot = None
    try:
        source_path = intake_storage.resolve_path(root or storage_root(), storage_path)
        snapshot = tempfile.TemporaryFile(mode="w+b")
        digest = hashlib.sha256()
        size = 0
        with source_path.open("rb") as source:
            while chunk := source.read(STREAM_CHUNK_BYTES):
                size += len(chunk)
                if size > size_bytes:
                    raise AttachmentRejected("Stored timeline attachment failed verification")
                digest.update(chunk)
                snapshot.write(chunk)
        if size != size_bytes or digest.hexdigest() != sha256:
            raise AttachmentRejected("Stored timeline attachment failed verification")
        snapshot.flush()
        snapshot.seek(0)
        return snapshot
    except AttachmentRejected:
        if snapshot is not None:
            snapshot.close()
        raise
    except (OSError, TypeError, ValueError, KeyError) as exc:
        if snapshot is not None:
            snapshot.close()
        raise AttachmentRejected("Stored timeline attachment failed verification") from exc


def safe_download_filename(filename: str) -> str:
    """Reduce source names to a single safe leaf before HTTP header encoding."""
    if not isinstance(filename, str):
        return "attachment"
    leaf = filename.replace("\\", "/").rsplit("/", 1)[-1]
    safe = "".join(
        char for char in leaf
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    ).strip(" .")[:255]
    return safe or "attachment"


def safe_content_type(content_type: str) -> str:
    """Use a bare syntactically safe media type; otherwise force download as bytes."""
    if isinstance(content_type, str) and _MEDIA_TYPE_RE.fullmatch(content_type):
        return content_type
    return "application/octet-stream"
