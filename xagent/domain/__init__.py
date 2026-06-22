"""Pure domain data structures for xAgent."""

from .attachments import (
    ATTACHMENT_METADATA_KEY,
    MAX_ATTACHMENT_BYTES,
    MAX_MESSAGE_ATTACHMENT_BYTES,
    attachment_image_sources,
    dedupe_attachments,
    workspace_attachment_from_path,
)
from .message_records import MessageBatch, StoredMessage
from .messages import (
    AgentTurnResult,
    Message,
    MessageType,
    ParticipationDecision,
    RoleType,
)
from .skills import SkillMetadata, SkillValidationIssue

__all__ = [
    "ATTACHMENT_METADATA_KEY",
    "AgentTurnResult",
    "MAX_ATTACHMENT_BYTES",
    "MAX_MESSAGE_ATTACHMENT_BYTES",
    "Message",
    "MessageBatch",
    "MessageType",
    "ParticipationDecision",
    "RoleType",
    "SkillMetadata",
    "SkillValidationIssue",
    "StoredMessage",
    "attachment_image_sources",
    "dedupe_attachments",
    "workspace_attachment_from_path",
]
