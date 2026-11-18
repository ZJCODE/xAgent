"""Idle wakes use the ordinary agent engine and durable operational state."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime

from ..inbox import InboxKind
from .task_receipts import sync_task_directory


class AutonomousWake:
    def __init__(self, loop):
        self.loop = loop
        self.agent = loop._agent
        self.path = loop._workspace / ".runtime" / "autonomous.json"
        self.state = {"next_run_at": time.time() + loop._interval}
        if self.path.exists():
            # A corrupt operational record must not silently enable replay.
            saved = json.loads(self.path.read_text())
            # Scheduling/delivery state only. Work history belongs in diary and receipts.
            keys = {"next_run_at", "last_exit", "error",
                    "active_wake", "pending_notification", "tool_results"}
            self.state.update({key: value for key, value in saved.items() if key in keys})
            # Do not inherit a longer delay from the removed idle-backoff policy.
            self.state["next_run_at"] = min(self.state["next_run_at"], time.time() + loop._interval)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "x", encoding="utf-8") as output:
                os.chmod(temporary, 0o600)
                json.dump(self.state, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
            sync_task_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    async def flush_notification(self, contacts, version):
        pending = self.state.get("pending_notification")
        if not pending or pending.get("status") == "needs_review":
            return
        loop = self.loop
        recipient = next((c for c in contacts if loop._contact_key(c.channel, c.user_id) == pending["message"]["recipient"]), None)
        if recipient is None or not loop._filter_deliverable_contacts([recipient]):
            return
        # Weixin acknowledges durable enqueue with a stable timestamp identity.
        # Other transports must not replay a send interrupted after it started.
        if pending.get("status") == "sending" and recipient.channel != "weixin":
            pending["status"] = "needs_review"
            self.save()
            return
        pending["status"] = "sending"
        self.save()
        from .subconscious import SubconsciousDelivery
        try:
            await loop._deliver_with_retries(SubconsciousDelivery(pending["message"]["text"], recipient,
                "", datetime.fromisoformat(pending["created_at"])), expected_version=version)
        except BaseException:
            if recipient.channel != "weixin":
                pending["status"] = "needs_review"
            self.save()
            raise
        self.state.pop("pending_notification", None)
        self.save()

    async def tick(self):
        loop = self.loop
        version = loop._request_version()
        if loop._thinking or loop._is_agent_busy():
            return
        from .subconscious import load_contacts
        contacts = loop._filter_deliverable_contacts(load_contacts(loop.contacts_file))
        if self.state.get("pending_notification"):
            await self.flush_notification(contacts, version)
        if time.time() < self.state["next_run_at"]:
            return
        loop._thinking = True
        wake_id = uuid.uuid4().hex
        self.state.update(active_wake=wake_id, next_run_at=time.time() + loop._interval)
        final = ""
        turn_id = None
        tool_count = 0
        tool_events = []
        result = None
        note_status = "none"
        try:
            self.save()
            stream = self.agent.chat_events("An idle reflection is due.",
                user_id="__autonomous__", channel="autonomous",
                inbox_kind=InboxKind.AUTONOMOUS_TURN,
                max_agent_loops=min(12, self.agent.max_agent_loops), event_id=wake_id,
                extra_message_metadata={"source": "autonomous", "wake_id": wake_id})
            try:
                async for event in stream:
                    turn_id = event.get("turn_id") or turn_id
                    if event.get("type") in {"error", "aborted"}:
                        raise RuntimeError("Autonomous turn stopped: " + str(event.get("error") or event.get("type")))
                    if event.get("type") in {"tool_call", "tool_result"}:
                        tool_events.append(event)
                        if turn_id:
                            await self.agent.turn_store.update(turn_id, internal_tool_events=tool_events)
                    if event.get("type") == "tool_result":
                        tool_count += 1
                    if event.get("type") == "message_done" and event.get("phase", "final") == "final":
                        final = str(event.get("content") or "")
            finally:
                await stream.aclose()
            result = loop._parse_subconscious_json(final)
            self.state.update(tool_results=tool_count, last_exit="completed", error=None)
            if turn_id:
                await self.agent.turn_store.update(turn_id, autonomous_result=result, autonomous_tool_results=tool_count)
            # Persist execution outcome independently of diary and notification I/O.
            self.state.pop("active_wake", None)
            if result["message"] and not self.state.get("pending_notification"):
                self.state["pending_notification"] = {"message": result["message"], "created_at": datetime.now().isoformat(), "status": "queued"}
            self.save()
            if loop._request_version() == version:
                try:
                    await self.flush_notification(contacts, version)
                except Exception:
                    # As in reflect, outward delivery and the private note are
                    # independent. Keep the pending receipt for recovery.
                    loop._logger.warning("Reflection notification failed; pending receipt retained", exc_info=True)
            if result["note"]:
                note_status = await loop._write_subconscious_thought(result["note"])
        except asyncio.CancelledError:
            self.state.update(last_exit="interrupted")
            self.save()
            if loop._request_version() == version:
                raise
        except Exception as exc:
            self.state.update(last_exit="failed", error=type(exc).__name__)
            self.save()
            loop._logger.warning("Autonomous wake failed; recorded work will be checked before continuing", exc_info=True)
        finally:
            loop._thinking = False
            loop._logger.info("Autonomous wake=%s turn=%s tools=%s note=%s exit=%s",
                              wake_id, turn_id, tool_count, note_status, self.state.get("last_exit"))
