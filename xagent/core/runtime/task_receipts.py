"""Execution safety and per-part delivery acknowledgement contexts."""
from __future__ import annotations

import os
import contextvars
from contextlib import contextmanager
from pathlib import Path


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


def sync_task_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PreparationError(RuntimeError):
    """A model/preparation failure with a machine-readable retry decision."""
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


_current_run = contextvars.ContextVar("xagent_current_task_run", default=None)


@contextmanager
def task_run_context(store, run, now):
    token = _current_run.set((store, run, now))
    try:
        yield
    finally:
        _current_run.reset(token)


def note_task_tool(name: str, safety: str) -> None:
    """Persist before starting a tool; never infer safety from a tool's name."""
    current = _current_run.get()
    if current is None:
        return
    store, run, now = current
    run.setdefault("tools", []).append({"name": name, "safety": safety, "started_at": now().isoformat()})
    if safety not in {"read_only", "idempotent"}:
        run["unsafe_tools_started"] = True
    store.save_run(run)


async def journal_delivery(part_id, operation, *, description="", uncertain_on_error=True, payload=""):
    """Confirm each send separately; sending without confirmation is never replayed."""
    import hashlib
    current = _current_run.get()
    if current is None:
        return await operation()
    store, run, now = current
    part = store.delivery(run["id"], part_id)
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    if part and part.get("fingerprint") != fingerprint:
        raise ValueError("Delivery content changed after preparation; cannot safely resend")
    if part and part["status"] == "accepted":
        return part.get("receipt") or {}
    if part and part["status"] in {"sending", "needs_review"} and part.get("uncertain_on_error", True):
        raise DeliveryUncertainError(f"Previous send is unconfirmed: {description or part_id}")
    part = {"id": part_id, "status": "sending", "description": description, "started_at": now().isoformat(), "uncertain_on_error": uncertain_on_error, "fingerprint": fingerprint}
    store.save_delivery(run["id"], part)
    try:
        result = await operation()
    except BaseException as exc:
        part.update(status="needs_review" if uncertain_on_error else "failed", error=type(exc).__name__)
        store.save_delivery(run["id"], part)
        raise
    part.update(status="accepted", receipt=result if isinstance(result, dict) else {}, finished_at=now().isoformat())
    store.save_delivery(run["id"], part)
    return result
