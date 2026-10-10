"""Operational turn receipts; raw messages and diary remain the memory sources."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class TurnStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._records: dict[str, dict] = {}
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("""CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                    record_json TEXT NOT NULL
                )""")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def begin(self, *, event_key: str, turn_id: str | None = None, **metadata) -> tuple[dict, bool]:
        return await asyncio.to_thread(self._begin, event_key, turn_id, metadata)

    def _begin(self, event_key, turn_id, metadata):
        record = {
            **metadata, "event_key": event_key, "turn_id": turn_id or uuid.uuid4().hex,
            "state": "accepting", "created_at": time.time(), "updated_at": time.time(),
            "events": [],
        }
        if self.path is None:
            existing = self._records.get(event_key)
            if existing is not None:
                return dict(existing), False
            self._records[event_key] = record
            return dict(record), True
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT record_json FROM turns WHERE event_key=?", (event_key,)).fetchone()
            if row:
                return json.loads(row[0]), False
            connection.execute("INSERT INTO turns VALUES(?,?,?)", (
                record["turn_id"], event_key, json.dumps(record, ensure_ascii=False),
            ))
        return record, True

    async def update(self, turn_id: str, **changes) -> None:
        await asyncio.to_thread(self._update, turn_id, changes)

    def _update(self, turn_id, changes):
        if self.path is None:
            for record in self._records.values():
                if record["turn_id"] == turn_id:
                    record.update(changes, updated_at=time.time())
                    return
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT record_json FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
            if row is None:
                return
            record = json.loads(row[0])
            record.update(changes, updated_at=time.time())
            connection.execute("UPDATE turns SET record_json=? WHERE turn_id=?", (
                json.dumps(record, ensure_ascii=False), turn_id,
            ))

    async def recover(self) -> int:
        """Never replay work after a crash; its external effects are unknown."""
        records = await self.list_records()
        recovered = 0
        for record in records:
            if record["state"] in {"accepting", "accepted", "running"}:
                await self.update(record["turn_id"], state="interrupted", needs_review=True)
                recovered += 1
        return recovered

    async def list_records(self) -> list[dict]:
        return await asyncio.to_thread(self._list_records)

    def _list_records(self):
        if self.path is None:
            return [dict(record) for record in self._records.values()]
        with self._connect() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM turns")]

    async def get_status(self) -> dict:
        records = await self.list_records()
        states: dict[str, int] = {}
        for record in records:
            states[record["state"]] = states.get(record["state"], 0) + 1
        return {"states": states, "needs_review": sum(bool(row.get("needs_review")) for row in records)}
