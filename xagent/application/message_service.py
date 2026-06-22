"""Message persistence service."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .message_images import MessageImageNormalizer
from ..ports import MessageStore
from ..domain import Message, RoleType, MessageType
from ..domain.attachments import (
    ATTACHMENT_METADATA_KEY,
    attachment_manifest_markdown,
    dedupe_attachments,
)
from ..infrastructure.media.images import extract_image_urls_from_text


class MessageService:
    """Store and retrieve messages for one agent stream."""

    def __init__(
        self,
        message_store: MessageStore,
        workspace_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.message_store = message_store
        self.image_normalizer = MessageImageNormalizer(workspace_dir=workspace_dir)

    async def store_user_message(
        self,
        user_message: str,
        user_id: str,
        image_source: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Message:
        normalized_attachments = dedupe_attachments(list(attachments or []))
        message_content = self.append_attachment_manifest(user_message, normalized_attachments)
        image_sources = self.image_normalizer.merge_sources(
            message_content,
            image_source,
            attachments=normalized_attachments,
        )
        normalized_sources, image_metadata = self.image_normalizer.prepare_message_images(image_sources)
        normalized_attachments = dedupe_attachments([
            *normalized_attachments,
            *MessageImageNormalizer.attachments_from_image_metadata(image_metadata),
        ])
        message_content = self.append_attachment_manifest(user_message, normalized_attachments)

        msg = Message.create(
            content=message_content,
            role=RoleType.USER,
            image_source=normalized_sources or None,
            sender_id=user_id,
        )
        if normalized_attachments:
            msg.metadata[ATTACHMENT_METADATA_KEY] = normalized_attachments
        if image_metadata:
            msg.metadata["images"] = image_metadata
        await self.message_store.add_messages(msg)
        return msg

    async def store_model_reply(
        self,
        reply_text: str,
        sender_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Message:
        normalized_attachments = dedupe_attachments(list(attachments or []))
        image_source = extract_image_urls_from_text(reply_text)
        model_msg = Message.create(
            content=reply_text,
            role=RoleType.ASSISTANT,
            sender_id=sender_id,
        )
        if metadata:
            model_msg.metadata.update(metadata)
        if normalized_attachments:
            model_msg.metadata[ATTACHMENT_METADATA_KEY] = normalized_attachments
        image_metadata = self.image_normalizer.preview_metadata(image_source)
        if image_metadata and "images" not in model_msg.metadata:
            model_msg.metadata["images"] = image_metadata
        await self.message_store.add_messages(model_msg)
        return model_msg

    async def store_context_event(
        self,
        context: str,
        source: str = "environment",
        event_type: str = "observation",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Message:
        event_msg = Message.create_context_event(
            content=context,
            source=source,
            event_type=event_type,
            metadata=metadata,
        )
        await self.message_store.add_messages(event_msg)
        return event_msg

    async def get_recent_messages(self, max_history: int) -> List[Message]:
        return await self.message_store.get_messages(max_history)

    async def get_input_messages(self, max_history: int) -> list:
        messages = await self.get_recent_messages(max_history)
        return self.to_model_input(messages)

    @staticmethod
    def to_model_input(messages: List[Message]) -> list:
        return [msg.to_model_input() for msg in messages]

    @staticmethod
    def filter_conversation_messages(messages: List[Message]) -> List[Message]:
        return [
            msg for msg in messages
            if msg.type == MessageType.MESSAGE
            and msg.role in (RoleType.USER, RoleType.ASSISTANT)
        ]

    @staticmethod
    def append_attachment_manifest(user_message: str, attachments: List[Dict[str, Any]]) -> str:
        content = str(user_message or "").strip()
        if not attachments:
            return content
        pending = []
        for attachment in attachments:
            blob_url = str(attachment.get("blob_url") or "").strip()
            path = str(attachment.get("path") or "").strip()
            if path and path in content:
                continue
            if blob_url and blob_url in content and not path:
                continue
            pending.append(attachment)
        manifest = attachment_manifest_markdown(pending)
        if not manifest:
            return content
        return f"{content}\n\n{manifest}" if content else manifest

    @staticmethod
    def sanitize_input_messages(input_messages: list) -> list:
        while input_messages and (
            input_messages[0].get("type") == "function_call_output"
            or input_messages[0].get("role") == "tool"
        ):
            input_messages.pop(0)
        return input_messages

    @staticmethod
    def filter_non_tool_messages(messages: list) -> list:
        return [
            msg for msg in messages
            if msg.get("role") in (RoleType.USER.value, RoleType.ASSISTANT.value)
        ]
