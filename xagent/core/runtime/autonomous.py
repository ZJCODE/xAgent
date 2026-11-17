"""Idle wakes use the ordinary agent engine and durable operational state."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import datetime

from ..inbox import InboxKind
from .task_receipts import sync_task_directory

AUTONOMOUS_INSTRUCTIONS = """This is an autonomous wake, not a human message or a new authorization.
Use the normal tools to advance existing user-delegated work. Read current state before
changing it. Your prior thoughts are not authorization or evidence that work is incomplete.
Respect existing user scope and tool rules. Verify outcomes against actual tool results.
Review the previous wake below, especially interrupted or uncertain effects, before resuming.
Do not repeat an unchanged check or blocked action. Stop when there is no useful next step.
Foreground requests take priority. Do not create recurring tasks to bypass this wake's budget.
Process text is private. Select an exact available recipient only when a useful notification
is needed; never infer an audience from the most recent speaker. Avoid duplicate notifications.
Tools are available throughout this turn. Your final response must be one JSON object:
{"outcome":"completed|progressed|blocked|idle", "summary":"brief factual result",
 "next_step":null, "blocked_on":null, "note":null, "message":null}
next_step and blocked_on are text or null. note is a genuinely new diary insight or null.
message is null or {"recipient":"exact identifier", "text":"notification"}.
Do not write routine wake summaries or repeated self-criticism into the diary.
"""


class AutonomousWake:
    def __init__(self, loop):
        self.loop = loop
        self.agent = loop._agent
        self.path = loop._workspace / ".runtime" / "autonomous.json"
        self.version = loop._request_version()
        self.state = {"next_run_at": time.time() + loop._interval, "quiet_count": 0, "chain": 0}
        if self.path.exists():
            # A corrupt operational record must not silently enable replay.
            self.state.update(json.loads(self.path.read_text()))

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

    @staticmethod
    def parse(text):
        text = text.strip()
        if text.startswith("```") and text.endswith("```"):
            text = "\n".join(text.splitlines()[1:-1])
        result = json.loads(text)
        fields = {"outcome", "summary", "next_step", "blocked_on", "note", "message"}
        if not isinstance(result, dict) or set(result) != fields:
            raise ValueError("Invalid autonomous result fields")
        if result["outcome"] not in {"completed", "progressed", "blocked", "idle"}:
            raise ValueError("Invalid autonomous outcome")
        if not isinstance(result["summary"], str):
            raise ValueError("Invalid autonomous summary")
        for key in ("next_step", "blocked_on", "note"):
            if result[key] is not None and not isinstance(result[key], str):
                raise ValueError(f"Invalid autonomous {key}")
        message = result["message"]
        if message is not None and (not isinstance(message, dict) or set(message) != {"recipient", "text"}
                or any(not isinstance(value, str) or not value.strip() for value in message.values())):
            raise ValueError("Invalid autonomous notification")
        return result

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
        if version != self.version:
            self.version = version
            self.state.update(quiet_count=0, chain=0, next_run_at=min(self.state["next_run_at"], time.time() + loop._interval))
            self.save()
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
        previous = dict(self.state)
        self.state.update(active_wake=wake_id, tool_events=[], next_run_at=time.time() + loop._interval)
        final = ""
        turn_id = None
        tool_count = 0
        result = None
        try:
            self.save()
            prompt = json.dumps({"previous_wake": previous, "available_recipients": [
                {"recipient": loop._contact_key(c.channel, c.user_id)} for c in contacts]}, ensure_ascii=False)
            stream = self.agent.chat_events(prompt, user_id="__autonomous__", channel="autonomous",
                inbox_kind=InboxKind.AUTONOMOUS_TURN, channel_instructions=AUTONOMOUS_INSTRUCTIONS,
                max_agent_loops=min(12, self.agent.max_agent_loops), event_id=wake_id, extra_message_metadata={"source": "autonomous", "wake_id": wake_id})
            try:
                async for event in stream:
                    turn_id = event.get("turn_id") or turn_id
                    if event.get("type") in {"error", "aborted"}:
                        raise RuntimeError("Autonomous turn stopped: " + str(event.get("error") or event.get("type")))
                    if event.get("type") in {"tool_call", "tool_result"}:
                        self.state["tool_events"] = [*self.state.get("tool_events", []), event][-24:]
                        self.save()
                    if event.get("type") == "tool_result":
                        tool_count += 1
                    if event.get("type") == "message_done" and event.get("phase", "final") == "final":
                        final = str(event.get("content") or "")
            finally:
                await stream.aclose()
            result = self.parse(final)
            identity = [result["outcome"], result["blocked_on"]] if result["outcome"] in {"idle", "blocked"} else [result["outcome"], result["summary"], result["next_step"]]
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            repeated = digest == previous.get("fingerprint")
            quiet = result["outcome"] in {"idle", "blocked"} or repeated
            count = previous.get("quiet_count", 0) + 1 if quiet else 0
            chain = previous.get("chain", 0) + 1 if not quiet and result["outcome"] == "progressed" and result["next_step"] else 0
            delay = min(max(7200, loop._interval), loop._interval * (2 ** min(count, 8))) if quiet else 60 if 0 < chain < 3 else loop._interval
            self.state.update(last_result=result, fingerprint=digest, quiet_count=count, chain=chain,
                              next_run_at=time.time() + delay, tool_results=tool_count, last_exit=result["outcome"], error=None)
            if turn_id:
                await self.agent.turn_store.update(turn_id, autonomous_result=result, autonomous_tool_results=tool_count)
            # Save before diary and delivery so a crash cannot replay tools to repair formatting or notification.
            self.state.pop("active_wake", None)
            if result["message"] and not repeated and not self.state.get("pending_notification"):
                self.state["pending_notification"] = {"message": result["message"], "created_at": datetime.now().isoformat(), "status": "queued"}
            self.save()
            if result["note"] and not repeated and not quiet:
                normalized = " ".join(result["note"].split())
                if normalized not in self.state.get("recent_notes", []):
                    if await loop._write_subconscious_thought(result["note"]) == "written":
                        self.state["recent_notes"] = [*self.state.get("recent_notes", []), normalized][-12:]
                        self.save()
            if loop._request_version() == version:
                await self.flush_notification(contacts, version)
        except asyncio.CancelledError:
            self.state.update(last_exit="interrupted", next_run_at=time.time() + loop._interval)
            self.save()
            if loop._request_version() == version:
                raise
        except Exception as exc:
            count = previous.get("quiet_count", 0) + 1
            self.state.update(last_exit="failed", error=type(exc).__name__, quiet_count=count,
                              next_run_at=time.time() + min(max(7200, loop._interval), loop._interval * 2 ** min(count, 8)))
            self.save()
            loop._logger.warning("Autonomous wake failed; recorded work will be checked before continuing", exc_info=True)
        finally:
            loop._thinking = False
            loop._logger.info("Autonomous wake=%s turn=%s tools=%s outcome=%s", wake_id, turn_id, tool_count, result["outcome"] if result else self.state.get("last_exit"))
