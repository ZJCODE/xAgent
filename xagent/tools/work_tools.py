"""Agent-facing tools for background work: delegate, list, cancel.

The work runtime (store + runner) is owned by the runtime host, which
registers these tools on the agent at startup. Every read/control is scoped
to the calling conversation via the tool executor's call scope.
"""
from __future__ import annotations

from xagent.core.tooling.scope import current_tool_call_scope
from xagent.utils.tool_decorator import function_tool


def create_delegate_work_tool(work_store, work_runner):
    """Create a tool that hands a task to the background work pool."""

    @function_tool(
        name="delegate_background_work",
        description=(
            "Hand a task to a background worker so it runs without blocking "
            "this conversation. Use it when a task will take long or must "
            "survive the end of the current turn. The worker executes the "
            "goal in a separate turn with the same conversation context. "
            "Do not delegate trivial or quick actions. Do not wait for the "
            "result: report the work_id and let the user check it later."
        ),
        param_descriptions={
            "goal": "The complete instruction for the worker. Self-contained: "
                    "repeat any details the worker cannot see here.",
            "completion_criteria": "Optional concrete conditions that define "
                    "done (artifacts produced, checks passed, messages sent).",
        },
    )
    async def delegate_background_work(goal: str, completion_criteria: str = "") -> dict:
        user_id, channel, room_name, _kind = current_tool_call_scope()
        if not str(user_id or "").strip():
            return {"ok": False, "error": "No active conversation to attach the work to"}
        if work_runner is None or getattr(work_runner, "execute", None) is None:
            return {"ok": False, "error": "Background execution is not available in this runtime"}
        record = await work_store.create(
            channel=channel or "",
            user_id=user_id,
            room_name=room_name or "",
            source="delegated",
            goal=goal,
            completion_criteria=completion_criteria or "",
        )
        if not await work_runner.submit(record["id"]):
            await work_store.update(record["id"], status="failed",
                                    needs_review_reason="runner rejected the work")
            return {"ok": False, "error": "Work was not accepted by the runner"}
        return {"ok": True, "work_id": record["id"], "status": "queued"}

    return delegate_background_work


def create_list_work_tool(work_store):
    """Create a tool listing this conversation's background work."""

    @function_tool(
        name="list_background_work",
        description=(
            "List background work records for this conversation, newest "
            "first. Use it to answer questions about task progress. "
            "Statuses: queued, running, stopping, waiting_user, completed, "
            "failed, needs_review."
        ),
        retry_safety="read_only",
    )
    async def list_background_work() -> dict:
        user_id, channel, _room_name, _kind = current_tool_call_scope()
        if not str(user_id or "").strip():
            return {"ok": False, "error": "No active conversation scope"}
        records = await work_store.list_for(channel or "", user_id)
        return {"ok": True, "work": [
            {
                "work_id": record["id"],
                "status": record.get("status"),
                "goal": str(record.get("goal") or "")[:200],
                "completion_reason": record.get("completion_reason"),
                "needs_review_reason": record.get("needs_review_reason"),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
            }
            for record in sorted(records, key=lambda r: r.get("created_at") or 0, reverse=True)
        ]}

    return list_background_work


def create_cancel_work_tool(work_store, work_runner):
    """Create a tool that stops this conversation's background work."""

    @function_tool(
        name="cancel_background_work",
        description=(
            "Cancel background work that is queued or running. Stopping is "
            "cooperative: the worker finishes its current step first. The "
            "work_id comes from list_background_work or an earlier "
            "delegate_background_work result."
        ),
        param_descriptions={
            "work_id": "The background work id to cancel.",
        },
    )
    async def cancel_background_work(work_id: str) -> dict:
        user_id, channel, _room_name, _kind = current_tool_call_scope()
        try:
            record = await work_store.get(work_id)
        except KeyError:
            return {"ok": False, "error": f"Work not found: {work_id}"}
        if (str(record.get("user_id") or "") != str(user_id or "")
                or str(record.get("channel") or "") != str(channel or "").strip()):
            # Cross-conversation probes look like missing work.
            return {"ok": False, "error": f"Work not found: {work_id}"}
        stopping = await work_runner.cancel(work_id)
        return {"ok": True, "work_id": work_id, "stopping": stopping,
                "status": (await work_store.get(work_id)).get("status")}

    return cancel_background_work
