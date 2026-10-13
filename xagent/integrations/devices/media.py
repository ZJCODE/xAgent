"""Streaming device files into the workspace with a private, durable index."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sqlite3
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

from PIL import Image

from ...schemas.attachment import attachment_kind, safe_attachment_filename, workspace_attachment_from_path
from ...utils.image_utils import SUPPORTED_UPLOAD_IMAGE_MIME_TYPES, detect_image_mime
from .config import DeviceMediaConfig


class MediaError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def attachment_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise MediaError(422, "invalid_attachment_id", "attachment_id must be a UUID") from None


async def disk_work(function, *args):
    """Finish a disk operation before propagating cancellation to its cleanup."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A worker thread cannot be cancelled. Never unlink a file while it is
        # still being written or release its quota before its commit finishes.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise


@dataclass
class Upload:
    attachment_id: str
    device_id: str
    owner: str
    file_name: str
    mime_type: str
    size_bytes: int
    path: str
    temporary: Path
    existing: dict | None = None
    reserved: bool = False


class DeviceMediaStore:
    def __init__(self, workspace: Path, state_dir: Path, config: DeviceMediaConfig):
        self.workspace = workspace.resolve()
        self.state_dir = state_dir / ".device-media"
        self.database = self.state_dir / "index.sqlite3"
        self.config = config
        self._lock = asyncio.Lock()
        self._prepared = False

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _path(self, relative: str) -> Path:
        path = (self.workspace / relative).resolve()
        if not path.is_relative_to(self.workspace):
            raise MediaError(403, "invalid_path", "Media path must stay inside the workspace")
        return path

    def _prepare(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attachments (
                attachment_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                owner TEXT NOT NULL, file_name TEXT NOT NULL, mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, path TEXT NOT NULL UNIQUE,
                sha256 TEXT, state TEXT NOT NULL
            )""")
            self.database.chmod(0o600)
            for row in db.execute("SELECT path FROM attachments WHERE state = 'uploading'").fetchall():
                try:
                    self._path(row["path"]).unlink(missing_ok=True)
                except MediaError:
                    pass  # Never follow a workspace directory replaced by a symlink.
            db.execute("DELETE FROM attachments WHERE state = 'uploading'")
            self._reconcile(db)
        for temporary in self.state_dir.glob("*.part"):
            temporary.unlink(missing_ok=True)

    def _reconcile(self, db):
        for row in db.execute("SELECT attachment_id, path FROM attachments WHERE state = 'ready'").fetchall():
            try:
                exists = self._path(row["path"]).is_file()
            except MediaError:
                exists = False
            if not exists:
                db.execute("DELETE FROM attachments WHERE attachment_id = ?", (row["attachment_id"],))

    async def prepare(self):
        async with self._lock:
            if not self._prepared:
                await disk_work(self._prepare)
                self._prepared = True

    async def recover(self):
        """Reconcile disk state when the Runtime reopens device admission."""
        async with self._lock:
            await disk_work(self._prepare)
            self._prepared = True

    def _reserve(self, upload: Upload):
        with self._connect() as db:
            self._reconcile(db)
            row = db.execute("SELECT * FROM attachments WHERE attachment_id = ?", (upload.attachment_id,)).fetchone()
            if row is not None:
                if row["device_id"] != upload.device_id or row["owner"] != upload.owner:
                    raise MediaError(409, "attachment_conflict", "Attachment ID is already in use")
                if row["state"] != "ready":
                    raise MediaError(409, "upload_in_progress", "Attachment is still uploading")
                if any(row[key] != getattr(upload, key) for key in ("file_name", "mime_type", "size_bytes")):
                    raise MediaError(409, "attachment_conflict", "Attachment metadata cannot be changed")
                upload.existing = dict(row)
                return
            total = db.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM attachments").fetchone()[0]
            device_total = db.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM attachments WHERE device_id = ?", (upload.device_id,)).fetchone()[0]
            if total + upload.size_bytes > self.config.quota_bytes or device_total + upload.size_bytes > self.config.device_quota_bytes:
                raise MediaError(429, "media_quota", "Device media storage quota exceeded")
            db.execute("INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'uploading')", (
                upload.attachment_id, upload.device_id, upload.owner, upload.file_name,
                upload.mime_type, upload.size_bytes, upload.path,
            ))
        upload.reserved = True

    def _validate_file(self, upload: Upload):
        with upload.temporary.open("rb") as handle:
            header = handle.read(32)
        detected = detect_image_mime(header)
        suffix = Path(upload.file_name).suffix.lower()
        looks_image = detected or upload.mime_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg"}
        if not looks_image:
            return
        if upload.size_bytes > self.config.max_image_bytes:
            raise MediaError(413, "image_too_large", "Image exceeds the device image size limit")
        if detected not in SUPPORTED_UPLOAD_IMAGE_MIME_TYPES or upload.mime_type != detected:
            raise MediaError(415, "unsupported_image", "Images must contain valid PNG, JPEG or WebP data with the matching Content-Type")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(upload.temporary) as image:
                    image.verify()
                with Image.open(upload.temporary) as image:
                    image.load()
        except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise MediaError(415, "invalid_image", "Image cannot be safely decoded") from exc

    def _finish(self, upload: Upload, sha256: str, authorized: Callable[[], bool]):
        if not authorized():
            raise MediaError(401, "device_unauthorized", "Device credential was revoked")
        if upload.existing is not None:
            if upload.existing["sha256"] != sha256:
                raise MediaError(409, "attachment_conflict", "Attachment content cannot be changed")
            upload.temporary.unlink(missing_ok=True)
            return
        target = self._path(upload.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # The file is synced before publishing; the index is the completion
        # marker. A crash between rename and commit is recovered on startup.
        with upload.temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        if not authorized():
            raise MediaError(401, "device_unauthorized", "Device credential was revoked")
        os.replace(upload.temporary, target)
        parent = target.parent
        while parent.is_relative_to(self.workspace):
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if parent == self.workspace:
                break
            parent = parent.parent
        with self._connect() as db:
            if not authorized():
                raise MediaError(401, "device_unauthorized", "Device credential was revoked")
            db.execute("UPDATE attachments SET sha256 = ?, state = 'ready' WHERE attachment_id = ?", (sha256, upload.attachment_id))

    def _abort(self, upload: Upload):
        upload.temporary.unlink(missing_ok=True)
        if upload.reserved:
            with self._connect() as db:
                row = db.execute("SELECT state FROM attachments WHERE attachment_id = ?", (upload.attachment_id,)).fetchone()
                if row is not None and row["state"] == "uploading":
                    try:
                        self._path(upload.path).unlink(missing_ok=True)
                    except MediaError:
                        pass
                    db.execute("DELETE FROM attachments WHERE attachment_id = ?", (upload.attachment_id,))

    async def upload(self, *, device_id: str, owner: str, attachment_id: str,
                     file_name: str, mime_type: str, size_bytes: int, sha256: str | None,
                     chunks: AsyncIterator[bytes], authorized: Callable[[], bool]) -> tuple[dict, bool]:
        attachment_id = attachment_uuid(attachment_id)
        if size_bytes <= 0:
            raise MediaError(400, "empty_attachment", "Attachment data cannot be empty")
        mime_type = mime_type.split(";", 1)[0].strip().lower()
        if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", mime_type):
            raise MediaError(415, "invalid_content_type", "A valid Content-Type is required")
        limit = self.config.max_image_bytes if mime_type.startswith("image/") else self.config.max_file_bytes
        if size_bytes > limit:
            raise MediaError(413, "attachment_too_large", "Attachment exceeds the device upload size limit")
        if sha256 is not None and not re.fullmatch(r"[a-fA-F0-9]{64}", sha256):
            raise MediaError(422, "invalid_checksum", "X-Content-SHA256 must be a SHA-256 hex digest")
        file_name = safe_attachment_filename(file_name or "attachment.bin")
        image_suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime_type)
        stored_name = f"content.{image_suffix}" if image_suffix else file_name
        await self.prepare()
        upload = Upload(attachment_id, device_id, owner, file_name, mime_type, size_bytes,
                        f"assets/inbound/devices/{attachment_id}/{stored_name}",
                        self.state_dir / f"{attachment_id}-{uuid.uuid4().hex}.part")
        try:
            async with self._lock:
                await disk_work(self._reserve, upload)
            await disk_work(upload.temporary.touch, 0o600)

            async def receive():
                digest = hashlib.sha256()
                received = 0
                iterator = chunks.__aiter__()
                last_data = asyncio.get_running_loop().time()
                while True:
                    try:
                        remaining = self.config.idle_timeout_seconds - (asyncio.get_running_loop().time() - last_data)
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        chunk = await asyncio.wait_for(iterator.__anext__(), remaining)
                    except StopAsyncIteration:
                        break
                    if not authorized():
                        raise MediaError(401, "device_unauthorized", "Device credential was revoked")
                    if not chunk:
                        continue
                    last_data = asyncio.get_running_loop().time()
                    received += len(chunk)
                    if received > size_bytes:
                        raise MediaError(400, "length_mismatch", "Body exceeds Content-Length")
                    # Bound writes even if an ASGI server supplies a large chunk.
                    for offset in range(0, len(chunk), 64 * 1024):
                        block = chunk[offset:offset + 64 * 1024]
                        await disk_work(self._append, upload.temporary, block, digest)
                if received != size_bytes:
                    raise MediaError(400, "length_mismatch", "Body does not match Content-Length")
                actual_digest = digest.hexdigest()
                if sha256 is not None and actual_digest != sha256.lower():
                    raise MediaError(400, "checksum_mismatch", "Attachment checksum does not match")
                await disk_work(self._validate_file, upload)
                await disk_work(self._finish, upload, actual_digest, authorized)
                return actual_digest

            try:
                # DeviceService owns the overall deadline, including recovery,
                # reservation and cleanup. This timeout is only receive-idle.
                actual_digest = await receive()
            except asyncio.TimeoutError:
                raise MediaError(408, "upload_timeout", "Device upload timed out") from None
            return {"attachment_id": attachment_id, "file_name": file_name,
                    "mime_type": mime_type, "size_bytes": size_bytes, "sha256": actual_digest}, upload.existing is None
        finally:
            async with self._lock:
                await disk_work(self._abort, upload)

    @staticmethod
    def _append(path: Path, data: bytes, digest):
        with path.open("ab") as handle:
            handle.write(data)
        digest.update(data)

    def _resolve(self, device_id: str, owner: str, attachment_ids: list[str]):
        with self._connect() as db:
            self._reconcile(db)
            attachments = []
            for attachment_id in dict.fromkeys(attachment_ids):
                row = db.execute("SELECT * FROM attachments WHERE attachment_id = ? AND device_id = ? AND owner = ? AND state = 'ready'", (attachment_id, device_id, owner)).fetchone()
                if row is None:
                    raise MediaError(400, "invalid_attachment", "Attachment is missing, incomplete or belongs to another pairing")
                attachment = workspace_attachment_from_path(
                    self._path(row["path"]), self.workspace, source_channel="devices",
                    source_resource_id=attachment_id, source_resource_type="device_media",
                )
                attachment.update(file_name=row["file_name"], kind=attachment_kind(row["mime_type"], row["file_name"]),
                                  mime_type=row["mime_type"], size_bytes=row["size_bytes"],
                                  source_device_id=device_id, sha256=row["sha256"])
                attachments.append(attachment)
            if sum(item["size_bytes"] for item in attachments) > self.config.max_message_bytes:
                raise MediaError(400, "attachment_limit", "Message attachments exceed the total size limit")
            return attachments

    async def resolve(self, device_id: str, owner: str, attachment_ids: list[str]) -> list[dict]:
        if len(attachment_ids) > self.config.max_attachments:
            raise MediaError(400, "attachment_limit", "Too many message attachments")
        if not attachment_ids:
            return []
        await self.prepare()
        async with self._lock:
            return await disk_work(self._resolve, device_id, owner, attachment_ids)
