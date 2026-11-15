"""Durable, size-bounded staging for outgoing Telegram attachments."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import secrets
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from topsbottg.models import ChatAttachment, ChatMessageOutbox

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
STAGING_QUOTA_BYTES = 256 * 1024 * 1024
STAGING_MAX_FILES = 1024
ORPHAN_TTL = timedelta(hours=24)
_KEY = re.compile(r"[0-9a-f]{48}\Z")
_staging_lock = asyncio.Lock()

MIME_EXTENSIONS = {
    "application/pdf": {".pdf"},
    "application/zip": {".zip"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {".pptx"},
    "text/plain": {".txt"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "audio/ogg": {".ogg", ".oga"},
}


class UploadRejected(ValueError):
    def __init__(self, code: str, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True)
class StagedUpload:
    key: str
    size: int
    digest: str
    kind: str
    mime_type: str
    filename: str


def staging_path(root: str | Path, key: str) -> Path:
    if _KEY.fullmatch(key) is None:
        raise ValueError("invalid staging key")
    return Path(root) / key


def remove_staged_file(root: str | Path, key: str) -> None:
    with suppress(OSError):
        staging_path(root, key).unlink(missing_ok=True)


def _canonical_name(filename: str | None, kind: str) -> str:
    name = (filename or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127).strip(" .")[:128]
    if not name:
        if kind == "photo":
            return "photo.jpg"
        if kind == "voice":
            return "voice.ogg"
        raise UploadRejected("invalid_filename", 415)
    return name


def _validate_payload(path: Path, *, kind: str, mime: str, filename: str) -> None:
    suffix = Path(filename).suffix.lower()
    if mime not in MIME_EXTENSIONS or suffix not in MIME_EXTENSIONS[mime]:
        raise UploadRejected("unsupported_file_type", 415)
    if kind == "photo" and mime not in {"image/jpeg", "image/png"}:
        raise UploadRejected("unsupported_photo", 415)
    if kind == "voice" and mime != "audio/ogg":
        raise UploadRejected("unsupported_voice", 415)
    if kind == "document" and mime == "audio/ogg":
        raise UploadRejected("unsupported_document", 415)
    with path.open("rb") as handle:
        header = handle.read(1024)
        if not header:
            raise UploadRejected("empty_file", 415)
        if mime == "image/jpeg":
            valid = header.startswith(b"\xff\xd8\xff")
        elif mime == "image/png":
            valid = header.startswith(b"\x89PNG\r\n\x1a\n")
        elif mime == "application/pdf":
            valid = header.startswith(b"%PDF-")
        elif mime == "audio/ogg":
            valid = header.startswith(b"OggS") and b"OpusHead" in header[:128]
        elif mime == "text/plain":
            valid = b"\x00" not in header
            if valid:
                try:
                    path.read_text(encoding="utf-8")
                except UnicodeError:
                    valid = False
        else:
            valid = zipfile.is_zipfile(path) and header.startswith(b"PK")
            if valid and mime != "application/zip":
                directory = {
                    ".docx": "word/",
                    ".xlsx": "xl/",
                    ".pptx": "ppt/",
                }[suffix]
                try:
                    with zipfile.ZipFile(path) as archive:
                        names = archive.namelist()
                        valid = (
                            len(names) <= 10000
                            and "[Content_Types].xml" in names
                            and any(item.startswith(directory) for item in names)
                        )
                except (OSError, zipfile.BadZipFile):
                    valid = False
    if not valid:
        raise UploadRejected("file_signature_mismatch", 415)


def _usage(root: Path) -> tuple[int, int]:
    total = count = 0
    for file in root.iterdir():
        if not _KEY.fullmatch(file.name) or file.is_symlink() or not file.is_file():
            continue
        total += file.stat().st_size
        count += 1
    return total, count


async def _lock_directory(root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / ".staging.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
    return fd


def _unlock_directory(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


async def stage_upload(file: UploadFile, *, kind: str, root: str | Path) -> StagedUpload:
    if kind not in {"photo", "document", "voice"}:
        raise UploadRejected("unsupported_kind", 415)
    mime = (file.content_type or "").split(";", 1)[0].strip().lower()
    filename = _canonical_name(file.filename, kind)
    if mime not in MIME_EXTENSIONS or Path(filename).suffix.lower() not in MIME_EXTENSIONS[mime]:
        raise UploadRejected("unsupported_file_type", 415)
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise UploadRejected("file_too_large", 413)
    if kind == "photo" and mime not in {"image/jpeg", "image/png"}:
        raise UploadRejected("unsupported_photo", 415)
    if kind == "voice" and mime != "audio/ogg":
        raise UploadRejected("unsupported_voice", 415)
    if kind == "document" and mime == "audio/ogg":
        raise UploadRejected("unsupported_document", 415)

    root = Path(root)
    key = secrets.token_hex(24)
    path = staging_path(root, key)
    async with _staging_lock:
        fd = await _lock_directory(root)
        try:
            used, count = _usage(root)
            if count >= STAGING_MAX_FILES:
                raise UploadRejected("staging_quota_exceeded", 507)
            size = 0
            digest = hashlib.sha256()
            handle_fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(handle_fd, "wb") as handle:
                    while True:
                        chunk = await file.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            raise UploadRejected("file_too_large", 413)
                        if used + size > STAGING_QUOTA_BYTES:
                            raise UploadRejected("staging_quota_exceeded", 507)
                        handle.write(chunk)
                        digest.update(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                _validate_payload(path, kind=kind, mime=mime, filename=filename)
                if kind == "photo" and size > 10 * 1024 * 1024:
                    raise UploadRejected("telegram_photo_too_large", 413)
                return StagedUpload(
                    key=key, size=size, digest=digest.hexdigest(),
                    kind=kind, mime_type=mime, filename=filename,
                )
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        finally:
            _unlock_directory(fd)


async def cleanup_staging(root: str | Path, session_factory: async_sessionmaker) -> int:
    """Never remove a file referenced by a queued or leased delivery."""
    root = Path(root)
    if not root.exists():
        return 0
    async with session_factory() as session:
        rows = (await session.execute(
            select(ChatAttachment.staging_key, ChatMessageOutbox.status)
            .join(ChatMessageOutbox, ChatMessageOutbox.message_id == ChatAttachment.chat_message_id)
            .where(ChatAttachment.staging_key.is_not(None))
        )).all()
    active = {key for key, status in rows if status in ("queued", "sending")}
    terminal = {key for key, status in rows if status in ("sent", "failed")}
    cutoff = datetime.now(UTC) - ORPHAN_TTL
    deleted = 0
    async with _staging_lock:
        fd = await _lock_directory(root)
        try:
            for item in root.iterdir():
                if _KEY.fullmatch(item.name) is None or item.is_symlink() or not item.is_file():
                    continue
                if item.name in active:
                    continue
                try:
                    if item.name not in terminal and datetime.fromtimestamp(item.stat().st_mtime, tz=UTC) > cutoff:
                        # Uncommitted uploads might have no database row yet.
                        continue
                    item.unlink()
                    deleted += 1
                except OSError:
                    continue
        finally:
            _unlock_directory(fd)
    return deleted
