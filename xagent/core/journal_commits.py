"""Durable operational receipts for Markdown diary windows.

Receipts are recovery data, never an alternative source of Agent memory.
One pending window is enough: the maintenance lock serializes all commits.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class JournalConflictError(ValueError):
    """The page changed while its replacement was being flushed."""


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def sync_directory(path: Path) -> None:
    """Make a rename durable on the supported macOS/Linux filesystems."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, content: bytes, *, expected: bytes | None = None) -> None:
    # The first diary page can create year/month directories. Flush each new
    # directory's entry in its parent before relying on a file inside it.
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        sync_directory(directory.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if expected is not None and read_bytes(path) != expected:
            raise JournalConflictError("Diary page changed while preparing its replacement")
        os.replace(temporary_path, path)
        sync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


class JournalCommitStore:
    """Keep the current window receipt outside searchable Markdown scopes."""

    def __init__(self, memory_root: Path) -> None:
        self.root = memory_root.resolve()
        self.path = self.root / ".journal_commits" / "current.json"

    def load(self) -> dict[str, Any] | None:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        receipt = json.loads(content)
        self.validate(receipt)
        return receipt

    def validate(self, receipt: dict[str, Any]) -> None:
        if not isinstance(receipt, dict) or receipt.get("version") != 1:
            raise ValueError("Unsupported journal commit receipt")
        if receipt.get("status") not in {"prepared", "completed", "needs_review"}:
            raise ValueError("Invalid journal commit state")
        if not isinstance(receipt.get("commit_id"), str) or not receipt["commit_id"]:
            raise ValueError("Missing journal commit identifier")
        for field in ("cursor_before", "start_exclusive", "end_inclusive", "old_length", "prepared_length"):
            if not isinstance(receipt.get(field), int) or receipt[field] < 0:
                raise ValueError(f"Invalid journal commit {field}")
        if not receipt["start_exclusive"] <= receipt["cursor_before"] <= receipt["end_inclusive"]:
            raise ValueError("Invalid source cursor bounds")
        target_date = date.fromisoformat(receipt["target_date"])
        expected = Path("daily") / str(target_date.year) / target_date.strftime("%Y-%m") / f"{target_date}.md"
        if receipt["target_path"] != expected.as_posix():
            raise ValueError("Invalid journal commit target")
        if not isinstance(receipt.get("prepared_content"), str):
            raise ValueError("Invalid prepared diary content")
        prepared_bytes = receipt["prepared_content"].encode("utf-8")
        if len(prepared_bytes) != receipt["prepared_length"]:
            raise ValueError("Prepared diary content length does not match")
        if digest(prepared_bytes) != receipt["prepared_sha256"]:
            raise ValueError("Prepared diary content checksum does not match")
        if not isinstance(receipt.get("old_sha256"), str) or len(receipt["old_sha256"]) != 64:
            raise ValueError("Invalid prior diary checksum")

    def prepare(
        self, *, target_date: date, old_content: bytes, prepared_content: str,
        cursor_before: int, start_exclusive: int, end_inclusive: int,
    ) -> dict[str, Any]:
        previous = self.load()
        if previous and previous["status"] != "completed":
            raise ValueError("Unresolved journal commit must be recovered before preparing another window")
        target_path = Path("daily") / str(target_date.year) / target_date.strftime("%Y-%m") / f"{target_date}.md"
        receipt = {
            "version": 1,
            "commit_id": uuid4().hex,
            "status": "prepared",
            "created_at": datetime.now().astimezone().isoformat(),
            "cursor_before": cursor_before,
            "start_exclusive": start_exclusive,
            "end_inclusive": end_inclusive,
            "target_date": target_date.isoformat(),
            "target_path": target_path.as_posix(),
            "old_sha256": digest(old_content),
            "old_length": len(old_content),
            "prepared_content": prepared_content,
            "prepared_length": len(prepared_content.encode("utf-8")),
            "prepared_sha256": digest(prepared_content.encode("utf-8")),
        }
        self.save(receipt)
        return receipt

    def save(self, receipt: dict[str, Any]) -> None:
        self.validate(receipt)
        atomic_write_bytes(
            self.path,
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def apply(self, receipt: dict[str, Any]) -> bool:
        """Apply once, or recognize an already applied exact append.

        A changed prior page or an ambiguous append is quarantined rather than
        replacing user edits. Return False while this window needs review.
        """
        self.validate(receipt)
        target = self.root / receipt["target_path"]
        if not target.resolve().is_relative_to(self.root / "daily"):
            raise ValueError("Diary target escapes the daily directory")
        current = read_bytes(target)
        old_length = receipt["old_length"]
        prepared = receipt["prepared_content"].encode("utf-8")
        unchanged = len(current) == old_length and digest(current) == receipt["old_sha256"]
        already_written = (
            len(current) == old_length + len(prepared)
            and digest(current[:old_length]) == receipt["old_sha256"]
            and current[old_length:] == prepared
        )
        if already_written:
            if prepared:
                # An earlier attempt may have failed after rename but before
                # directory fsync. Finish durability before its cursor commit.
                sync_directory(target.parent)
            return True
        if unchanged:
            if prepared:
                try:
                    atomic_write_bytes(target, current + prepared, expected=current)
                except JournalConflictError:
                    pass
                else:
                    return True
            else:
                return True
        receipt["status"] = "needs_review"
        receipt["reason"] = "Diary page changed after this window was prepared"
        self.save(receipt)
        return False

    def complete(self, receipt: dict[str, Any]) -> None:
        receipt["status"] = "completed"
        receipt["completed_at"] = datetime.now().astimezone().isoformat()
        self.save(receipt)
