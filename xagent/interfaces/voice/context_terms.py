"""Speech-recognition context terms taken from what the agent already knows.

Getting a person's name right on the first try is the single largest
contributor to a voice device feeling intelligent, and the agent already
knows who it talks to: contacts carry display names, relationship cards carry
a name in their frontmatter. Asking the user to retype those into config.yaml
would create a second, hand-maintained copy of the agent's own memory, which
the diary-anchored memory principle exists to prevent.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .attention import normalize_terms

logger = logging.getLogger(__name__)

MAX_CONTEXT_TERMS = 50


def agent_identity_terms(agent: Any) -> list[str]:
    """The agent's own name, so it recognises being addressed."""
    terms: list[str] = []
    for attr in ("display_name", "name"):
        value = getattr(agent, attr, None)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    identity = getattr(agent, "identity", None) or getattr(agent, "system_prompt", None)
    if isinstance(identity, str):
        first_line = identity.strip().splitlines()[0] if identity.strip() else ""
        if first_line and len(first_line) <= 64:
            terms.append(first_line)
    return normalize_terms(terms)


def contact_terms(agent: Any) -> list[str]:
    """Display names recorded for people the agent has talked to."""
    workspace = getattr(agent, "workspace", None)
    if not workspace:
        return []
    try:
        from ...core.runtime import load_contacts, resolve_contacts_path
        from ...components.memory.relationship_memory import human_display_name

        contacts = load_contacts(resolve_contacts_path(Path(workspace)))
    except Exception:
        logger.debug("Could not read contacts for voice context terms", exc_info=True)
        return []
    terms: list[str] = []
    for entry in contacts:
        target = entry.target if isinstance(entry.target, dict) else {}
        for key in ("sender_name", "display_name"):
            name = human_display_name(target.get(key), user_id=entry.user_id)
            if name:
                terms.append(name)
    return normalize_terms(terms)


async def known_people_terms(agent: Any) -> list[str]:
    """Names from relationship cards, which outlive any single channel."""
    store = getattr(agent, "relationship_store", None)
    if store is None:
        return []
    try:
        from ...components.memory.relationship_memory import human_display_name

        keys = await store.list_keys()
        cards = await store.read_cards(list(keys))
    except Exception:
        logger.debug("Could not read relationship cards for voice context terms", exc_info=True)
        return []
    terms: list[str] = []
    for card in cards:
        name = human_display_name(card.display_name, user_id=card.user_id, key=card.key)
        if name:
            terms.append(name)
    return normalize_terms(terms)


def startup_context_terms(agent: Any) -> list[str]:
    """Terms available without awaiting anything, for the first STT session."""
    return normalize_terms(agent_identity_terms(agent) + contact_terms(agent))[
        :MAX_CONTEXT_TERMS
    ]


async def refreshed_context_terms(agent: Any) -> list[str]:
    """Everything the agent knows about who it speaks with."""
    terms = agent_identity_terms(agent) + await known_people_terms(agent) + contact_terms(agent)
    return normalize_terms(terms)[:MAX_CONTEXT_TERMS]
