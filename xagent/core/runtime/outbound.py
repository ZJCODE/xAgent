"""File-backed outbound intents: agent-initiated communication with a person.

OutboundIntent is the core primitive for "address someone". Conscious turns
(reach_out) and subconscious reflection are equal producers; channel adapters
only drain and transport.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..config import AgentConfig
from .scheduler import _fsync_directory
from .subconscious import ContactEntry, load_contacts, resolve_contacts_path

logger = logging.getLogger(__name__)

OUTBOUND_DIRNAME = "outbound"
OUTBOUND_PENDING_DIRNAME = "pending"
OUTBOUND_DELIVERED_DIRNAME = "delivered"
OUTBOUND_FAILED_DIRNAME = "failed"
OUTBOUND_CLAIM_MARKER = ".claiming-"
OUTBOUND_PAYLOAD_VERSION = 1

OUTBOUND_SOURCE_CONSCIOUS = "conscious"
OUTBOUND_SOURCE_SUBCONSCIOUS = "subconscious"
OUTBOUND_STATUS_PENDING = "pending"
OUTBOUND_STATUS_DELIVERED = "delivered"
OUTBOUND_STATUS_FAILED = "failed"

SUPPORTED_OUTBOUND_SOURCES = {OUTBOUND_SOURCE_CONSCIOUS, OUTBOUND_SOURCE_SUBCONSCIOUS}


@dataclass(frozen=True)
class OutboundIntent:
    """An agent decision to communicate with a specific person."""

    intent_id: str
    content: str
    recipient: ContactEntry
    source: str
    created_at: datetime
    motive: Optional[str] = None
    requester_user_id: str = ""
    status: str = OUTBOUND_STATUS_PENDING
    internal_content: str = ""
    error: str = ""
    path: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": OUTBOUND_PAYLOAD_VERSION,
            "id": self.intent_id,
            "content": self.content,
            "recipient": {
                "channel": self.recipient.channel,
                "user_id": self.recipient.user_id,
                "target": dict(self.recipient.target),
                "last_seen": self.recipient.last_seen,
                "interaction_count": self.recipient.interaction_count,
            },
            "source": self.source,
            "motive": self.motive,
            "requester_user_id": self.requester_user_id,
            "created_at": self.created_at.replace(microsecond=0).isoformat(sep=" "),
            "status": self.status,
            "internal_content": self.internal_content,
            "error": self.error,
        }


@dataclass(frozen=True)
class RecipientResolution:
    """Result of resolving a person_ref against known contacts."""

    match: Optional[ContactEntry] = None
    candidates: tuple[ContactEntry, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.match is not None and not self.error


def resolve_outbound_root(workspace: Path | str) -> Path:
    """Return the outbound queue root under an agent workspace."""
    return Path(workspace).expanduser().resolve() / OUTBOUND_DIRNAME


def ensure_outbound_dirs(workspace: Path | str) -> Path:
    """Create outbound pending/delivered/failed directories and return the root."""
    root = resolve_outbound_root(workspace)
    for name in (OUTBOUND_PENDING_DIRNAME, OUTBOUND_DELIVERED_DIRNAME, OUTBOUND_FAILED_DIRNAME):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def _pending_dir(root: Path) -> Path:
    return root / OUTBOUND_PENDING_DIRNAME


def _delivered_dir(root: Path) -> Path:
    return root / OUTBOUND_DELIVERED_DIRNAME


def _failed_dir(root: Path) -> Path:
    return root / OUTBOUND_FAILED_DIRNAME


def _parse_created_at(raw: Any) -> datetime:
    text = str(raw or "").strip()
    if not text:
        return datetime.now().replace(microsecond=0)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(microsecond=0)
    except ValueError:
        return datetime.now().replace(microsecond=0)


def _contact_from_payload(raw: Any) -> ContactEntry:
    data = raw if isinstance(raw, dict) else {}
    return ContactEntry(
        channel=str(data.get("channel") or ""),
        user_id=str(data.get("user_id") or ""),
        target=dict(data.get("target") or {}) if isinstance(data.get("target"), dict) else {},
        last_seen=str(data.get("last_seen") or ""),
        interaction_count=int(data.get("interaction_count") or 0),
    )


def intent_from_payload(payload: Dict[str, Any], *, path: Optional[Path] = None) -> OutboundIntent:
    """Build an OutboundIntent from a persisted JSON payload."""
    return OutboundIntent(
        intent_id=str(payload.get("id") or "").strip() or uuid.uuid4().hex,
        content=str(payload.get("content") or ""),
        recipient=_contact_from_payload(payload.get("recipient")),
        source=str(payload.get("source") or OUTBOUND_SOURCE_CONSCIOUS).strip() or OUTBOUND_SOURCE_CONSCIOUS,
        created_at=_parse_created_at(payload.get("created_at")),
        motive=(str(payload["motive"]).strip() if payload.get("motive") is not None else None),
        requester_user_id=str(payload.get("requester_user_id") or ""),
        status=str(payload.get("status") or OUTBOUND_STATUS_PENDING),
        internal_content=str(payload.get("internal_content") or ""),
        error=str(payload.get("error") or ""),
        path=path,
    )


def _write_intent_file(path: Path, intent: OutboundIntent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    payload = intent.to_dict()
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)


def _load_intent_file(path: Path) -> Optional[OutboundIntent]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return intent_from_payload(raw, path=path)


def enqueue_outbound(
    workspace: Path | str,
    *,
    content: str,
    recipient: ContactEntry,
    source: str,
    motive: Optional[str] = None,
    requester_user_id: str = "",
    internal_content: str = "",
    created_at: Optional[datetime] = None,
    intent_id: Optional[str] = None,
) -> OutboundIntent:
    """Persist a pending outbound intent for channel transport to drain."""
    text = str(content or "").strip()
    if not text:
        raise ValueError("outbound content must be non-empty")
    channel = str(recipient.channel or "").strip().lower()
    if not channel:
        raise ValueError("outbound recipient.channel is required")
    normalized_source = str(source or "").strip().lower()
    if normalized_source not in SUPPORTED_OUTBOUND_SOURCES:
        raise ValueError(f"unsupported outbound source: {source!r}")

    root = ensure_outbound_dirs(workspace)
    now = (created_at or datetime.now()).replace(microsecond=0)
    intent = OutboundIntent(
        intent_id=str(intent_id or uuid.uuid4().hex),
        content=text,
        recipient=ContactEntry(
            channel=channel,
            user_id=str(recipient.user_id or ""),
            target=dict(recipient.target or {}),
            last_seen=str(recipient.last_seen or ""),
            interaction_count=int(recipient.interaction_count or 0),
        ),
        source=normalized_source,
        created_at=now,
        motive=(str(motive).strip() or None) if motive is not None else None,
        requester_user_id=str(requester_user_id or ""),
        status=OUTBOUND_STATUS_PENDING,
        internal_content=str(internal_content or "").strip(),
    )
    path = _pending_dir(root) / f"{intent.intent_id}.json"
    if path.exists():
        raise ValueError(f"outbound intent already exists: {intent.intent_id}")
    _write_intent_file(path, intent)
    return OutboundIntent(
        intent_id=intent.intent_id,
        content=intent.content,
        recipient=intent.recipient,
        source=intent.source,
        created_at=intent.created_at,
        motive=intent.motive,
        requester_user_id=intent.requester_user_id,
        status=intent.status,
        internal_content=intent.internal_content,
        path=path,
    )


def list_pending_outbound(
    workspace: Path | str,
    *,
    channel: Optional[str] = None,
) -> List[OutboundIntent]:
    """List pending intents, optionally filtered by recipient channel."""
    root = ensure_outbound_dirs(workspace)
    wanted = str(channel or "").strip().lower()
    intents: List[OutboundIntent] = []
    for path in sorted(_pending_dir(root).glob("*.json"), key=lambda item: item.name):
        if OUTBOUND_CLAIM_MARKER in path.name:
            continue
        intent = _load_intent_file(path)
        if intent is None:
            continue
        if wanted and str(intent.recipient.channel or "").strip().lower() != wanted:
            continue
        intents.append(intent)
    return intents


def claim_pending_outbound(
    workspace: Path | str,
    *,
    channel: str,
    limit: int = 8,
) -> List[OutboundIntent]:
    """Atomically claim pending intents for one transport channel."""
    wanted = str(channel or "").strip().lower()
    if not wanted:
        return []
    root = ensure_outbound_dirs(workspace)
    claimed: List[OutboundIntent] = []
    for path in sorted(_pending_dir(root).glob("*.json"), key=lambda item: item.name):
        if len(claimed) >= max(1, int(limit)):
            break
        if OUTBOUND_CLAIM_MARKER in path.name:
            continue
        intent = _load_intent_file(path)
        if intent is None:
            continue
        if str(intent.recipient.channel or "").strip().lower() != wanted:
            continue
        claim_path = path.with_name(f"{path.stem}{OUTBOUND_CLAIM_MARKER}{uuid.uuid4().hex}{path.suffix}")
        try:
            os.rename(path, claim_path)
        except OSError:
            continue
        claimed_intent = _load_intent_file(claim_path)
        if claimed_intent is None:
            try:
                os.rename(claim_path, path)
            except OSError:
                pass
            continue
        claimed.append(claimed_intent)
    return claimed


def _unique_destination(directory: Path, intent_id: str) -> Path:
    candidate = directory / f"{intent_id}.json"
    if not candidate.exists():
        return candidate
    return directory / f"{intent_id}-{uuid.uuid4().hex[:8]}.json"


def mark_delivered(intent: OutboundIntent, *, workspace: Optional[Path | str] = None) -> OutboundIntent:
    """Move a claimed/pending intent into the delivered archive."""
    return _finalize_intent(
        intent,
        status=OUTBOUND_STATUS_DELIVERED,
        error="",
        workspace=workspace,
    )


def mark_failed(
    intent: OutboundIntent,
    *,
    error: str,
    workspace: Optional[Path | str] = None,
) -> OutboundIntent:
    """Move a claimed/pending intent into the failed archive."""
    return _finalize_intent(
        intent,
        status=OUTBOUND_STATUS_FAILED,
        error=str(error or "delivery failed"),
        workspace=workspace,
    )


def _finalize_intent(
    intent: OutboundIntent,
    *,
    status: str,
    error: str,
    workspace: Optional[Path | str],
) -> OutboundIntent:
    if intent.path is None and workspace is None:
        raise ValueError("intent.path or workspace is required to finalize outbound intent")
    if intent.path is not None:
        # pending/id.json or pending/id.claiming-….json → outbound/
        outbound_root = intent.path.parent.parent
        agent_workspace = outbound_root.parent
    else:
        agent_workspace = Path(workspace).expanduser().resolve()  # type: ignore[arg-type]
        outbound_root = ensure_outbound_dirs(agent_workspace)
    ensure_outbound_dirs(agent_workspace)
    destination_dir = (
        _delivered_dir(outbound_root)
        if status == OUTBOUND_STATUS_DELIVERED
        else _failed_dir(outbound_root)
    )
    destination_dir.mkdir(parents=True, exist_ok=True)
    updated = OutboundIntent(
        intent_id=intent.intent_id,
        content=intent.content,
        recipient=intent.recipient,
        source=intent.source,
        created_at=intent.created_at,
        motive=intent.motive,
        requester_user_id=intent.requester_user_id,
        status=status,
        internal_content=intent.internal_content,
        error=error,
    )
    destination = _unique_destination(destination_dir, intent.intent_id)
    _write_intent_file(destination, updated)
    if intent.path is not None and intent.path.exists():
        try:
            intent.path.unlink()
        except OSError:
            logger.warning("Failed to remove outbound claim file %s", intent.path, exc_info=True)
    return OutboundIntent(
        intent_id=updated.intent_id,
        content=updated.content,
        recipient=updated.recipient,
        source=updated.source,
        created_at=updated.created_at,
        motive=updated.motive,
        requester_user_id=updated.requester_user_id,
        status=updated.status,
        internal_content=updated.internal_content,
        error=updated.error,
        path=destination,
    )


def resolve_recipient(
    person_ref: str,
    contacts: Sequence[ContactEntry],
    *,
    display_names: Optional[Dict[str, str]] = None,
    channel: Optional[str] = None,
) -> RecipientResolution:
    """Resolve a free-text person reference to a contact.

    Matching order: exact user_id, exact sender_name / display_name, then
    partial contains matches. Ambiguous partial matches return candidates.
    """
    hint = str(person_ref or "").strip()
    if not hint:
        return RecipientResolution(error="person_ref is empty")
    hint_lower = hint.lower()
    scoped = [
        contact
        for contact in contacts
        if not channel or str(contact.channel or "").strip().lower() == str(channel).strip().lower()
    ]
    if not scoped:
        return RecipientResolution(error="no contacts available")

    names = display_names or {}

    def _labels(contact: ContactEntry) -> List[str]:
        values = [
            contact.user_id,
            str(contact.target.get("sender_name") or ""),
            str(contact.target.get("user_id") or ""),
            names.get(f"{contact.channel}:{contact.user_id}", ""),
        ]
        return [str(value).strip() for value in values if str(value).strip()]

    exact: List[ContactEntry] = []
    for contact in scoped:
        for label in _labels(contact):
            if label.lower() == hint_lower:
                exact.append(contact)
                break
    if len(exact) == 1:
        return RecipientResolution(match=exact[0])
    if len(exact) > 1:
        return RecipientResolution(
            candidates=tuple(exact),
            error="ambiguous person_ref; multiple exact matches",
        )

    partial: List[ContactEntry] = []
    for contact in scoped:
        for label in _labels(contact):
            label_lower = label.lower()
            if hint_lower in label_lower or label_lower in hint_lower:
                partial.append(contact)
                break
    if len(partial) == 1:
        return RecipientResolution(match=partial[0])
    if len(partial) > 1:
        return RecipientResolution(
            candidates=tuple(partial),
            error="ambiguous person_ref; multiple partial matches",
        )
    return RecipientResolution(error=f"no contact matched person_ref={person_ref!r}")


def load_recipient_display_names(workspace: Path | str) -> Dict[str, str]:
    """Best-effort display names from relationship cards keyed by channel:user_id."""
    try:
        from ...components.memory import RelationshipStore
    except Exception:
        return {}
    memory_root = Path(workspace).expanduser().resolve() / AgentConfig.MEMORY_DIRNAME
    relationships_root = memory_root / AgentConfig.RELATIONSHIPS_DIRNAME
    if not relationships_root.is_dir():
        return {}
    try:
        store = RelationshipStore(relationships_root)
        names: Dict[str, str] = {}
        paths = store._list_paths_sync(store.root)
        for path in paths:
            text = store._read_text_sync(path)
            meta, _ = store._parse_meta(text)
            key = str(meta.get("key") or "").strip()
            display = str(meta.get("name") or "").strip()
            if key and display:
                names[key] = display
        return names
    except Exception:
        logger.debug("Failed to load relationship display names", exc_info=True)
        return {}


def contact_summary(contact: ContactEntry) -> Dict[str, Any]:
    """Compact contact view for tool results."""
    return {
        "channel": contact.channel,
        "user_id": contact.user_id,
        "sender_name": contact.target.get("sender_name") or "",
        "target": dict(contact.target),
    }


async def drain_outbound_once(
    workspace: Path | str,
    *,
    channels: Iterable[str],
    deliver,
    limit_per_channel: int = 8,
    logger_: Optional[logging.Logger] = None,
) -> int:
    """Claim and deliver pending intents for the given transport channels.

    ``deliver`` is ``async def deliver(intent: OutboundIntent) -> None``.
    """
    import inspect

    log = logger_ or logger
    delivered_count = 0
    for channel in channels:
        normalized = str(channel or "").strip().lower()
        if not normalized:
            continue
        claimed = claim_pending_outbound(
            workspace,
            channel=normalized,
            limit=limit_per_channel,
        )
        for intent in claimed:
            try:
                result = deliver(intent)
                if inspect.isawaitable(result):
                    await result
                mark_delivered(intent)
                delivered_count += 1
                log.info(
                    "Outbound delivered: id=%s channel=%s user_id=%s source=%s",
                    intent.intent_id,
                    intent.recipient.channel,
                    intent.recipient.user_id,
                    intent.source,
                )
            except Exception as exc:
                mark_failed(intent, error=str(exc))
                log.warning(
                    "Outbound delivery failed: id=%s channel=%s user_id=%s error=%s",
                    intent.intent_id,
                    intent.recipient.channel,
                    intent.recipient.user_id,
                    exc,
                    exc_info=True,
                )
    return delivered_count


# Re-export helpers commonly needed by producers.
__all__ = [
    "OUTBOUND_DIRNAME",
    "OUTBOUND_SOURCE_CONSCIOUS",
    "OUTBOUND_SOURCE_SUBCONSCIOUS",
    "OUTBOUND_STATUS_PENDING",
    "OUTBOUND_STATUS_DELIVERED",
    "OUTBOUND_STATUS_FAILED",
    "OutboundIntent",
    "RecipientResolution",
    "claim_pending_outbound",
    "contact_summary",
    "drain_outbound_once",
    "enqueue_outbound",
    "ensure_outbound_dirs",
    "intent_from_payload",
    "list_pending_outbound",
    "load_recipient_display_names",
    "mark_delivered",
    "mark_failed",
    "resolve_outbound_root",
    "resolve_recipient",
    "load_contacts",
    "resolve_contacts_path",
]
