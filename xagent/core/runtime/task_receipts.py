"""Small durable execution receipts; these are operational data, not memory."""
from __future__ import annotations

import json
import os
import uuid
import contextvars
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


class DeliveryUncertainError(RuntimeError):
    """A delivery may have reached its destination and must not be replayed."""


_strict_delivery = contextvars.ContextVar("xagent_strict_task_delivery", default=False)


@contextmanager
def strict_task_delivery():
    token = _strict_delivery.set(True)
    try:
        yield
    finally:
        _strict_delivery.reset(token)


def is_strict_task_delivery() -> bool:
    return _strict_delivery.get()


def occurrence_run_id(task_id: str, run_at: datetime) -> str:
    return f"{task_id}-{run_at.strftime('%Y%m%d-%H%M%S')}"


def sync_task_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_durable_task_directory(path: Path) -> None:
    missing = []
    directory = path
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        sync_task_directory(directory.parent)


class TaskReceiptStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        ensure_durable_task_directory(self.root)
        # Runtime ownership may have just created .runtime. Persist its entry
        # before any execution depends on a receipt inside task_runs.
        sync_task_directory(self.root.parent)
        sync_task_directory(self.root.parent.parent)

    def read(self, run_id: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path(run_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            raise ValueError(f"invalid task execution receipt: {run_id}")
        return value

    def path(self, run_id: str) -> Path:
        if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in run_id):
            raise ValueError("invalid task run_id")
        return self.root / f"{run_id}.json"

    def write(self, receipt: dict[str, Any]) -> None:
        path = self.path(str(receipt["run_id"]))
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(receipt, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def _sync_directory(self) -> None:
        sync_task_directory(self.root)
