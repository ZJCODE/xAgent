"""Durable background-work records and conservative crash recovery.

One SQLite row per work item (``work_id`` + JSON record), mirroring
TurnStore's shape. Work statuses track lifecycle, not execution health:
controls carry their own ``received``/``incorporated`` states and delivery
status is tracked separately by the delivery machinery.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

WORK_STATUSES = {"queued", "running", "stopping", "waiting_user", "completed", "failed", "needs_review"}
WORK_TERMINAL = {"completed", "failed", "needs_review"}
CONTROL_RECEIVED = "received"
CONTROL_INCORPORATED = "incorporated"


class WorkStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self._records: dict[str, dict] = {}
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("""CREATE TABLE IF NOT EXISTS work (
                    work_id TEXT PRIMARY KEY, record_json TEXT NOT NULL
                )""")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def create(self, *, channel: str, user_id: str, room_name: str = "", source: str,
                     goal: str, completion_criteria: str = "", context_refs: Optional[list] = None,
                     attachment_refs: Optional[list] = None, work_id: Optional[str] = None,
                     status: str = "queued", turn_ids: Optional[list] = None) -> dict:
        return await asyncio.to_thread(self._create, dict(
            channel=channel, user_id=user_id, room_name=room_name or "", source=source,
            goal=goal, completion_criteria=completion_criteria or "",
            context_refs=list(context_refs or []), attachment_refs=list(attachment_refs or []),
            status=status, turn_ids=list(turn_ids or []),
        ), work_id)

    def _create(self, fields: dict, work_id: Optional[str]) -> dict:
        now = time.time()
        record = {
            "id": work_id or uuid.uuid4().hex,
            "status": "queued", "turn_ids": [], "controls": [],
            "result_ref": None, "delivery": None, "resumable": False,
            "needs_review_reason": None, "resume_from": None,
            "created_at": now, "updated_at": now,
            **fields,
        }
        if self.path is None:
            self._records[record["id"]] = record
        else:
            with self._connect() as connection:
                connection.execute("INSERT INTO work VALUES(?,?)", (
                    record["id"], json.dumps(record, ensure_ascii=False),
                ))
        return dict(record)

    async def get(self, work_id: str) -> dict:
        return await asyncio.to_thread(self._get, work_id)

    def _get(self, work_id):
        if self.path is None:
            record = self._records.get(work_id)
            if record is None:
                raise KeyError(work_id)
            return dict(record)
        with self._connect() as connection:
            row = connection.execute("SELECT record_json FROM work WHERE work_id=?", (work_id,)).fetchone()
            if row is None:
                raise KeyError(work_id)
            return json.loads(row[0])

    async def list_for(self, channel: str, user_id: str) -> list[dict]:
        records = await asyncio.to_thread(self._list_all)
        return [
            record for record in records
            if str(record.get("channel") or "") == str(channel or "").strip()
            and str(record.get("user_id") or "") == str(user_id or "").strip()
        ]

    async def list_all(self) -> list[dict]:
        return await asyncio.to_thread(self._list_all)

    def _list_all(self):
        if self.path is None:
            return [dict(record) for record in self._records.values()]
        with self._connect() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM work")]

    async def update(self, work_id: str, **changes) -> None:
        await asyncio.to_thread(self._update, work_id, changes)

    def _update(self, work_id, changes):
        if self.path is None:
            record = self._records.get(work_id)
            if record is None:
                raise KeyError(work_id)
            record.update(changes, updated_at=time.time())
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT record_json FROM work WHERE work_id=?", (work_id,)).fetchone()
            if row is None:
                raise KeyError(work_id)
            record = json.loads(row[0])
            record.update(changes, updated_at=time.time())
            connection.execute("UPDATE work SET record_json=? WHERE work_id=?", (
                json.dumps(record, ensure_ascii=False), work_id,
            ))

    async def append_control(self, work_id: str, content: str) -> str:
        """Persist a supplement message and return its control id."""
        control = {
            "id": uuid.uuid4().hex, "content": str(content or ""),
            "state": CONTROL_RECEIVED, "created_at": time.time(),
        }
        await asyncio.to_thread(self._append_control, work_id, control)
        return control["id"]

    def _append_control(self, work_id, control):
        if self.path is None:
            record = self._records.get(work_id)
            if record is None:
                raise KeyError(work_id)
            record.setdefault("controls", []).append(control)
            record["updated_at"] = time.time()
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT record_json FROM work WHERE work_id=?", (work_id,)).fetchone()
            if row is None:
                raise KeyError(work_id)
            record = json.loads(row[0])
            record.setdefault("controls", []).append(control)
            record["updated_at"] = time.time()
            connection.execute("UPDATE work SET record_json=? WHERE work_id=?", (
                json.dumps(record, ensure_ascii=False), work_id,
            ))

    async def mark_control_incorporated(self, work_id: str, control_id: str) -> None:
        await asyncio.to_thread(self._mark_control, work_id, control_id)

    def _mark_control(self, work_id, control_id):
        if self.path is None:
            record = self._records.get(work_id)
            if record is None:
                raise KeyError(work_id)
            for control in record.get("controls") or []:
                if control["id"] == control_id:
                    control["state"] = CONTROL_INCORPORATED
                    record["updated_at"] = time.time()
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT record_json FROM work WHERE work_id=?", (work_id,)).fetchone()
            if row is None:
                raise KeyError(work_id)
            record = json.loads(row[0])
            for control in record.get("controls") or []:
                if control["id"] == control_id:
                    control["state"] = CONTROL_INCORPORATED
            record["updated_at"] = time.time()
            connection.execute("UPDATE work SET record_json=? WHERE work_id=?", (
                json.dumps(record, ensure_ascii=False), work_id,
            ))


async def recover_work(work_store: WorkStore, turn_store) -> int:
    """Resolve non-terminal work after a restart.

    Run before TurnStore.recover(): running turns are still in the accepting/
    running states here, so the per-turn resumability metadata is trustworthy.

    Rules:
    - ``queued``/terminal stay as they are;
    - ``stopping`` means the stop was acknowledged before the crash → completed;
    - ``waiting_user`` keeps waiting (the question is still pending);
    - ``running`` resumes only from the latest snapshot when the turn is
      resumable and no external side effect had started; otherwise the work
      lands in ``needs_review`` for a human decision.
    """
    list_records = getattr(turn_store, "list_records", None)
    turn_records = (
        {record["turn_id"]: record for record in await list_records()}
        if callable(list_records) else {}
    )
    recovered = 0
    for work in await work_store.list_all():
        status = work.get("status")
        if status in WORK_TERMINAL or status in {"queued", "waiting_user"}:
            continue
        recovered += 1
        if status == "stopping":
            await work_store.update(work["id"], status="completed", completion_reason="stopped")
            continue
        turn_ids = work.get("turn_ids") or []
        turn = turn_records.get(turn_ids[-1]) if turn_ids else None
        if turn is None:
            await work_store.update(work["id"], status="needs_review",
                                    needs_review_reason="active turn record missing")
        elif turn.get("resumable") and not turn.get("side_effect_started"):
            await work_store.update(work["id"], status="queued",
                                    resume_from=turn.get("resume_snapshot"))
        else:
            await work_store.update(work["id"], status="needs_review",
                                    needs_review_reason="unsafe or non-resumable interruption")
    return recovered
