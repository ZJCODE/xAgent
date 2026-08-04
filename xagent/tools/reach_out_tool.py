"""Built-in tool for agent-initiated communication with a known person."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from xagent.core.runtime import current_delivery_context
from xagent.core.runtime.outbound import (
    OUTBOUND_SOURCE_CONSCIOUS,
    contact_summary,
    enqueue_outbound,
    load_contacts,
    load_recipient_display_names,
    resolve_contacts_path,
    resolve_recipient,
)
from xagent.utils.tool_decorator import function_tool


def create_reach_out_tool(*, workspace: str):
    """Create a tool that enqueues an OutboundIntent to address someone."""
    agent_workspace = Path(workspace).expanduser().resolve()

    @function_tool(
        name="reach_out",
        description=(
            "Address a known person with your own outbound message. "
            "Use when you decide to initiate contact — including self-driven follow-ups "
            "or after judging that a relay request is appropriate. "
            "This records your speech to that person; it is not a remote-control forwarder. "
            "Resolve the person carefully; if ambiguous, ask which person before calling again."
        ),
        param_descriptions={
            "person_ref": "Name or user_id of the person to address.",
            "content": "The message you will send in your own voice.",
            "motive": "Optional short motive tag such as self, relay, follow_up, or care.",
        },
    )
    def reach_out(
        person_ref: str,
        content: str,
        motive: Optional[str] = None,
    ) -> dict:
        text = str(content or "").strip()
        if not text:
            return {"ok": False, "error": "content must be non-empty"}

        contacts_file = resolve_contacts_path(agent_workspace)
        contacts = load_contacts(contacts_file)
        display_names = load_recipient_display_names(agent_workspace)
        resolution = resolve_recipient(
            person_ref,
            contacts,
            display_names=display_names,
        )
        if not resolution.ok:
            payload = {
                "ok": False,
                "error": resolution.error or "recipient not resolved",
                "person_ref": person_ref,
            }
            if resolution.candidates:
                payload["candidates"] = [contact_summary(item) for item in resolution.candidates]
            return payload

        recipient = resolution.match
        assert recipient is not None
        context = current_delivery_context()
        requester_user_id = context.user_id if context is not None else ""
        try:
            intent = enqueue_outbound(
                agent_workspace,
                content=text,
                recipient=recipient,
                source=OUTBOUND_SOURCE_CONSCIOUS,
                motive=motive,
                requester_user_id=requester_user_id,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc), "person_ref": person_ref}

        return {
            "ok": True,
            "intent_id": intent.intent_id,
            "status": intent.status,
            "source": intent.source,
            "motive": intent.motive,
            "recipient": contact_summary(intent.recipient),
            "requester_user_id": intent.requester_user_id,
        }

    return reach_out
