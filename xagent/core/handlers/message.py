import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..config import AgentConfig
from ..formatters import RoomSnapshot
from ..inbox import INBOX_KIND_METADATA_KEY, InboxKind, is_scheduled_work, scheduled_task_display_content
from ...components import MessageStorage
from ...schemas import Message, RoleType, MessageType
from ...schemas.attachment import (
    ATTACHMENT_METADATA_KEY,
    attachment_image_sources,
    attachment_manifest_markdown,
    dedupe_attachments,
)
from ..context_budget import cap_message_content, trim_experience_entries
from ..context_manifest import ManifestEntry, manifest_entry_from_message
from ..prompt_registry import (
    KIND_DECISION,
    KIND_INSTRUCTIONS,
    KIND_TURN,
    PromptAssembleContext,
    PromptRegistry,
    default_prompt_registry,
)
from ...utils.image_utils import (
    MAX_IMAGES_PER_MESSAGE,
    ImageSourceType,
    bytes_to_data_uri,
    classify_source,
    data_uri_to_bytes,
    extract_image_urls_from_text,
    extract_source,
    extract_workspace_image_paths_from_text,
    infer_format,
    read_image_file_bytes,
    resolve_workspace_blob_path,
    resolve_workspace_file_path,
    save_image_bytes_to_workspace,
    workspace_blob_relative_path,
    workspace_blob_url,
    workspace_image_data_uri,
    workspace_path_is_image,
)

logger = logging.getLogger(__name__)


class MessageHandler:
    """Handles message storage, retrieval, sanitization, and system prompt building."""

    def __init__(
        self,
        message_storage: MessageStorage,
        system_prompt: str = "",
        operator_policy: str = "",
        workspace_dir: Optional[Union[str, Path]] = None,
        prompt_registry: Optional[PromptRegistry] = None,
    ):
        self.message_storage = message_storage
        self.system_prompt = system_prompt
        self.operator_policy = operator_policy or ""
        self.workspace_dir = Path(workspace_dir).expanduser().resolve() if workspace_dir is not None else None
        self.prompt_registry = prompt_registry or default_prompt_registry()

    async def store_user_message(
        self,
        user_message: str,
        user_id: str,
        image_source: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        room_name: Optional[str] = None,
        room_id: Optional[str] = None,
        source_event_id: Optional[str] = None,
        channel: Optional[str] = None,
        recipient_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        persist: bool = True,
    ) -> Message:
        """Store a user message, auto-detecting embedded image URLs and attachments.

        ``persist=False`` still builds the in-memory row (images, attachments)
        so a presence turn can attach them to current_task without writing a
        USER utterance into storage.
        """
        normalized_attachments = dedupe_attachments(list(attachments or []))
        message_content = self._append_attachment_manifest(user_message, normalized_attachments)
        image_sources = self._merge_image_sources(message_content, image_source)
        for source in attachment_image_sources(normalized_attachments):
            canonical = self._canonical_image_source(source)
            if canonical and canonical not in image_sources:
                image_sources.append(canonical)
        normalized_sources, image_metadata = self._prepare_message_images(image_sources)
        normalized_attachments = dedupe_attachments([
            *normalized_attachments,
            *self._attachments_from_image_metadata(image_metadata),
        ])
        message_content = self._append_attachment_manifest(user_message, normalized_attachments)

        msg = Message.create(
            content=message_content,
            role=RoleType.USER,
            image_source=normalized_sources or None,
            sender_id=user_id,
        )
        msg.recipient_id = recipient_id or "agent"
        if room_name:
            msg.room_name = room_name
        MessageHandler._stamp_message_identity(
            msg,
            room_id=room_id,
            source_event_id=source_event_id,
        )
        if channel:
            msg.channel = channel
        if metadata:
            msg.metadata.update(metadata)
        if normalized_attachments:
            msg.metadata[ATTACHMENT_METADATA_KEY] = normalized_attachments
        if image_metadata:
            msg.metadata["images"] = image_metadata
        if persist:
            await self.message_storage.add_messages(msg)
        return msg

    async def store_model_reply(
        self,
        reply_text: str,
        sender_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        room_name: Optional[str] = None,
        channel: Optional[str] = None,
        recipient_id: Optional[str] = None,
    ) -> Message:
        normalized_attachments = dedupe_attachments(list(attachments or []))
        image_source = extract_image_urls_from_text(reply_text)
        model_msg = Message.create(
            content=reply_text,
            role=RoleType.ASSISTANT,
            sender_id=sender_id,
        )
        if recipient_id:
            model_msg.recipient_id = recipient_id
        if room_name:
            model_msg.room_name = room_name
        if channel:
            model_msg.channel = channel
        if metadata:
            model_msg.metadata.update(metadata)
        if normalized_attachments:
            model_msg.metadata[ATTACHMENT_METADATA_KEY] = normalized_attachments
        image_metadata = self._preview_image_metadata(image_source)
        if image_metadata and "images" not in model_msg.metadata:
            model_msg.metadata["images"] = image_metadata
        await self.message_storage.add_messages(model_msg)
        return model_msg

    async def store_context_event(
        self,
        context: str,
        source: str = "environment",
        event_type: str = "observation",
        metadata: Optional[Dict[str, Any]] = None,
        room_name: Optional[str] = None,
        room_id: Optional[str] = None,
        source_event_id: Optional[str] = None,
        role: RoleType = RoleType.ENVIRONMENT,
        channel: Optional[str] = None,
        recipient_id: Optional[str] = None,
        sender_id: Optional[str] = None,
    ) -> Message:
        """Store a non-direct observation from the agent's environment."""
        event_msg = Message.create_context_event(
            content=context,
            source=source,
            event_type=event_type,
            metadata=metadata,
            role=role,
        )
        if sender_id:
            event_msg.sender_id = sender_id
        if recipient_id:
            event_msg.recipient_id = recipient_id
        if room_name:
            event_msg.room_name = room_name
        MessageHandler._stamp_message_identity(
            event_msg,
            room_id=room_id,
            source_event_id=source_event_id,
        )
        if channel:
            event_msg.channel = channel
        await self.message_storage.add_messages(event_msg)
        return event_msg

    async def get_recent_messages(
        self,
        limit: int,
    ) -> List[Message]:
        return await self.message_storage.get_messages(limit)

    @staticmethod
    def filter_conversation_messages(messages: List[Message]) -> List[Message]:
        """Keep only persisted user/assistant natural-language messages."""
        return [
            msg for msg in messages
            if msg.type == MessageType.MESSAGE
            and msg.role in (RoleType.USER, RoleType.ASSISTANT)
        ]

    @staticmethod
    def filter_context_events(messages: List[Message]) -> List[Message]:
        """Keep persisted environment observations/context events."""
        return [msg for msg in messages if msg.type == MessageType.CONTEXT_EVENT]

    @staticmethod
    def build_turn_context_messages(
        messages: List[Message],
        current_user_id: str,
        memory_context: str = "",
        relationship_context: str = "",
        notebook_context: str = "",
        context_events: Optional[List[Message]] = None,
        current_time: Optional[str] = None,
        current_date: Optional[str] = None,
        max_messages: int = AgentConfig.DEFAULT_RECENT_MESSAGES,
        max_context_events: int = AgentConfig.MAX_CONTEXT_EVENTS,
        include_images: bool = True,
        workspace_dir: Optional[Union[str, Path]] = None,
        current_message: Optional[Message] = None,
        channel_instructions: str = "",
        room_context: Union[str, RoomSnapshot, None] = "",
        task_mode: str = "reply",
        working_summary: str = "",
        covers_through_cursor: int = 0,
        experience_token_cap: int = 0,
        prompt_registry: Optional[PromptRegistry] = None,
    ) -> list[dict]:
        """Build the per-turn model input context as named message layers."""
        messages_out, _entries = MessageHandler.build_turn_context_with_manifest(
            messages,
            current_user_id,
            memory_context=memory_context,
            relationship_context=relationship_context,
            notebook_context=notebook_context,
            context_events=context_events,
            current_time=current_time,
            current_date=current_date,
            max_messages=max_messages,
            max_context_events=max_context_events,
            include_images=include_images,
            workspace_dir=workspace_dir,
            current_message=current_message,
            channel_instructions=channel_instructions,
            room_context=room_context,
            task_mode=task_mode,
            working_summary=working_summary,
            covers_through_cursor=covers_through_cursor,
            experience_token_cap=experience_token_cap,
            prompt_registry=prompt_registry,
        )
        return messages_out

    @staticmethod
    def build_turn_context_with_manifest(
        messages: List[Message],
        current_user_id: str,
        memory_context: str = "",
        relationship_context: str = "",
        notebook_context: str = "",
        context_events: Optional[List[Message]] = None,
        current_time: Optional[str] = None,
        current_date: Optional[str] = None,
        max_messages: int = AgentConfig.DEFAULT_RECENT_MESSAGES,
        max_context_events: int = AgentConfig.MAX_CONTEXT_EVENTS,
        include_images: bool = True,
        workspace_dir: Optional[Union[str, Path]] = None,
        current_message: Optional[Message] = None,
        channel_instructions: str = "",
        room_context: Union[str, RoomSnapshot, None] = "",
        task_mode: str = "reply",
        working_summary: str = "",
        covers_through_cursor: int = 0,
        experience_token_cap: int = 0,
        prompt_registry: Optional[PromptRegistry] = None,
    ) -> tuple[list[dict], list[ManifestEntry]]:
        """Build per-turn context layers and manifest entries for each non-empty section."""
        conversation_messages = MessageHandler.filter_conversation_messages(messages)
        observation_messages = (
            MessageHandler.filter_context_events(messages)
            if context_events is None
            else MessageHandler.filter_context_events(context_events)
        )
        budgeted_entries, omitted_count = MessageHandler._budget_transcript_entries(
            conversation_messages,
            max_messages=max_messages,
            covers_through_cursor=covers_through_cursor,
        )
        budgeted_messages = [msg for msg, _ in budgeted_entries]
        budgeted_observations, omitted_observation_count = MessageHandler._budget_context_events(
            observation_messages,
            max_events=max_context_events,
            covers_through_cursor=covers_through_cursor,
        )
        experience_entries = MessageHandler._merge_experience_entries(
            budgeted_entries,
            budgeted_observations,
        )
        room_snapshot, room_render = MessageHandler._resolve_room_context(room_context)
        # The trigger belongs to current_input, including presence turns. Only
        # remove an identified event in the same room; equal text is not identity.
        if (
            isinstance(room_context, RoomSnapshot)
            and current_message is not None
            and current_message.event_key
            and current_message.resolved_room_id == room_snapshot.room_id
        ):
            room_snapshot = replace(
                room_snapshot,
                entries=tuple(
                    entry for entry in room_snapshot.entries
                    if entry.event_id != current_message.event_key
                ),
            )
            room_render = room_snapshot.render()
        experience_entries = MessageHandler._exclude_covered_experience_entries(
            experience_entries,
            current_message=current_message,
            room_snapshot=room_snapshot,
            has_room_block=bool(room_render),
        )
        experience_entries = [
            (
                entry_type,
                msg,
                cap_message_content(content, AgentConfig.MAX_RAW_MESSAGE_CHARS),
            )
            for entry_type, msg, content in experience_entries
        ]
        unsummarized_dropped = 0
        if experience_token_cap > 0:
            experience_entries, unsummarized_dropped = trim_experience_entries(
                experience_entries,
                max_est_tokens=experience_token_cap,
                covers_through_cursor=covers_through_cursor,
                storage_cursor_fn=MessageHandler._storage_cursor,
            )

        recent_experience = MessageHandler._build_recent_experience_context(
            experience_entries=experience_entries,
            omitted_messages=omitted_count,
            omitted_observations=omitted_observation_count,
            working_summary=working_summary,
            unsummarized_dropped=unsummarized_dropped,
        )
        resolved_current_time = (
            current_time
            or current_date
            or datetime.now().strftime("%Y-%m-%d %H:%M")
        )
        inbox_kind = ""
        if current_message is not None:
            inbox_kind = str(
                (current_message.metadata or {}).get(INBOX_KIND_METADATA_KEY) or ""
            ).strip()
        current_input_content = MessageHandler._current_input_content(
            current_message,
            inbox_kind=inbox_kind,
        )
        room_label = ""
        if room_snapshot is not None:
            room_label = (room_snapshot.room_name or room_snapshot.room_id or "").strip()
        elif current_message is not None and (current_message.room_name or "").strip():
            room_label = str(current_message.room_name or "").strip()
        speaker_label = MessageHandler._speaker_address_for(
            conversation_messages,
            current_user_id,
            current_message=current_message,
        )
        ctx = PromptAssembleContext(
            relationship_context=relationship_context,
            memory_context=memory_context,
            notebook_context=notebook_context,
            recent_experience=recent_experience,
            current_user_id=speaker_label,
            current_time=resolved_current_time,
            channel_instructions=channel_instructions,
            room_context=room_render,
            room_label=room_label,
            has_room_snapshot=bool(room_render),
            current_input_content=current_input_content,
            task_mode=task_mode,
            inbox_kind=inbox_kind,
        )
        registry = prompt_registry or default_prompt_registry()
        context_messages, manifest_entries = registry.assemble_with_manifest(KIND_TURN, ctx)

        current_task_text = next(
            (
                str(message["content"])
                for message in context_messages
                if message.get("name") == AgentConfig.CURRENT_INPUT_NAME
                and isinstance(message.get("content"), str)
            ),
            "",
        )

        current_images: List[str] = []
        if include_images:
            image_message = current_message or MessageHandler._latest_current_user_message(
                conversation_messages,
                current_user_id,
            )
            current_images = MessageHandler._current_message_images(
                image_message,
                current_user_id,
                workspace_dir=workspace_dir,
            )
        if current_images and current_task_text:
            for message in context_messages:
                if message.get("name") != AgentConfig.CURRENT_INPUT_NAME:
                    continue
                content = [{"type": "text", "text": current_task_text}]
                content.extend(
                    {"type": "image_url", "image_url": {"url": image_source}}
                    for image_source in current_images
                )
                message["content"] = content
                break
            current_layer = next(
                (message for message in context_messages if message.get("name") == AgentConfig.CURRENT_INPUT_NAME),
                {},
            )
            MessageHandler._refresh_manifest_entry_for_message(
                manifest_entries,
                message_name=AgentConfig.CURRENT_INPUT_NAME,
                message=current_layer,
            )
        return context_messages, manifest_entries

    @staticmethod
    def _refresh_manifest_entry_for_message(
        entries: list[ManifestEntry],
        *,
        message_name: str,
        message: dict,
    ) -> None:
        if not message or message.get("name") != message_name:
            return
        for index, entry in enumerate(entries):
            if entry.name != message_name:
                continue
            entries[index] = manifest_entry_from_message(
                message,
                kind=entry.kind,
                trust=entry.trust,
                priority=entry.priority,
                authority=entry.authority,
            )
            return

    @staticmethod
    def count_current_task_images(messages: list[dict]) -> int:
        for message in messages:
            if message.get("name") != AgentConfig.CURRENT_INPUT_NAME:
                continue
            content = message.get("content")
            if not isinstance(content, list):
                return 0
            return sum(1 for block in content if isinstance(block, dict) and block.get("type") == "image_url")
        return 0

    @staticmethod
    def apply_see_image_paths(
        messages: list[dict],
        extra_sources: list[str],
        *,
        workspace_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        """Add queued workspace images onto current_task, the sole vision inject site."""
        if not extra_sources:
            return
        for message in messages:
            if message.get("name") != AgentConfig.CURRENT_INPUT_NAME:
                continue
            content = message.get("content")
            text = ""
            existing: List[dict] = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and not text:
                        text = str(block.get("text") or "")
                    elif block.get("type") == "image_url":
                        existing.append(block)
            else:
                text = str(content or "")
            seen_urls = {
                str((block.get("image_url") or {}).get("url") or "")
                for block in existing
            }
            new_blocks: List[dict] = []
            for source in extra_sources:
                if len(existing) + len(new_blocks) >= MAX_IMAGES_PER_MESSAGE:
                    break
                try:
                    image_url = MessageHandler._model_image_source(source, workspace_dir=workspace_dir)
                except ValueError:
                    continue
                if not image_url or image_url in seen_urls:
                    continue
                new_blocks.append({"type": "image_url", "image_url": {"url": image_url}})
                seen_urls.add(image_url)
            if not new_blocks:
                return
            message["content"] = [{"type": "text", "text": text}, *existing, *new_blocks]
            return

    @staticmethod
    def _build_recent_experience_context(
        experience_entries: List[tuple[str, Message, str]],
        omitted_messages: int,
        omitted_observations: int,
        working_summary: str = "",
        unsummarized_dropped: int = 0,
    ) -> str:
        lines: list[str] = []
        if unsummarized_dropped > 0:
            lines.append(
                f"[{unsummarized_dropped} earlier messages omitted; not yet summarized]"
            )
            lines.append("")
        summary = (working_summary or "").strip()
        if summary:
            lines.append("[Working context: since last diary entry]")
            lines.append(summary)
            lines.append("")
        elif omitted_messages or omitted_observations:
            lines.append(
                MessageHandler._format_omitted_experience_note(
                    omitted_messages=omitted_messages,
                    omitted_observations=omitted_observations,
                )
            )
            lines.append("")

        for entry_type, msg, content in experience_entries:
            lines.extend(
                MessageHandler._format_experience_entry(entry_type, msg, content)
            )
            lines.append("")

        experience_text = "\n".join(lines).strip() or "[No recent experience]"
        return MessageHandler._wrap_untrusted_context(
            AgentConfig.RECENT_EXPERIENCE_NAME,
            experience_text,
        )

    @staticmethod
    def _wrap_untrusted_context(tag_name: str, content: str) -> str:
        return (
            f"<{tag_name}>\n\n"
            f"{content.strip()}\n\n"
            f"</{tag_name}>"
        )

    @staticmethod
    def _merge_experience_entries(
        conversation_entries: List[tuple[Message, str]],
        observation_entries: List[tuple[Message, str]],
    ) -> List[tuple[str, Message, str]]:
        entries = [
            ("message", msg, content)
            for msg, content in conversation_entries
        ]
        entries.extend(
            ("observation", msg, content)
            for msg, content in observation_entries
        )
        return sorted(entries, key=lambda entry: entry[1].timestamp)

    @staticmethod
    def _format_omitted_experience_note(
        omitted_messages: int,
        omitted_observations: int,
    ) -> str:
        parts: list[str] = []
        if omitted_messages:
            noun = "message" if omitted_messages == 1 else "messages"
            parts.append(f"{omitted_messages} conversation {noun}")
        if omitted_observations:
            noun = "observation" if omitted_observations == 1 else "observations"
            parts.append(f"{omitted_observations} {noun}")
        return "[Earlier experience omitted: " + ", ".join(parts) + "]"

    @staticmethod
    def _stamp_message_identity(
        message: Message,
        *,
        room_id: Optional[str] = None,
        source_event_id: Optional[str] = None,
    ) -> None:
        if room_id:
            message.room_id = str(room_id).strip() or None
            if message.room_id:
                message.metadata[AgentConfig.ROOM_ID_METADATA_KEY] = message.room_id
        if source_event_id:
            message.source_event_id = str(source_event_id).strip() or None
            if message.source_event_id:
                message.metadata[AgentConfig.SOURCE_EVENT_ID_METADATA_KEY] = message.source_event_id

    @staticmethod
    def _resolve_room_context(
        room_context: Union[str, RoomSnapshot, None],
    ) -> tuple[Optional[RoomSnapshot], str]:
        if isinstance(room_context, RoomSnapshot):
            return room_context, room_context.render()
        text = str(room_context or "").strip()
        if not text:
            return None, ""
        snapshot = RoomSnapshot.from_legacy_text(text)
        if snapshot.room_id:
            return snapshot, text
        return None, text

    @staticmethod
    def _current_input_content(
        current_message: Optional[Message],
        *,
        inbox_kind: str = "",
    ) -> str:
        if current_message is None:
            return ""
        kind = str(inbox_kind or "").strip()
        if kind == InboxKind.SCHEDULED_TURN.value:
            return scheduled_task_display_content(
                current_message.content,
                current_message.metadata,
            )
        return (current_message.content or "").strip()

    @staticmethod
    def _exclude_covered_experience_entries(
        experience_entries: List[tuple[str, Message, str]],
        *,
        current_message: Optional[Message],
        room_snapshot: Optional[RoomSnapshot],
        has_room_block: bool = False,
    ) -> List[tuple[str, Message, str]]:
        covered_keys = room_snapshot.event_keys if room_snapshot else set()
        snapshot_room_id = (room_snapshot.room_id if room_snapshot else "") or ""
        current_key = current_message.event_key if current_message else None

        filtered: list[tuple[str, Message, str]] = []
        for entry in experience_entries:
            _, msg, _ = entry
            if current_message is not None and MessageHandler._is_same_message(msg, current_message):
                continue
            key = msg.event_key
            if current_key and key and key == current_key:
                continue
            if covered_keys and key and key in covered_keys:
                msg_room = msg.resolved_room_id or ""
                if snapshot_room_id and msg_room == snapshot_room_id:
                    continue
            if (
                not covered_keys
                and has_room_block
                and current_message is not None
                and MessageHandler._is_same_message(msg, current_message)
            ):
                continue
            filtered.append(entry)
        return filtered

    @staticmethod
    def _is_same_message(message: Message, other: Optional[Message]) -> bool:
        """Identity check that works for the freshly stored copy and the reloaded row."""
        if other is None:
            return False
        left_key = message.event_key
        right_key = other.event_key
        if left_key and right_key and left_key == right_key:
            return True
        return (
            message.timestamp == other.timestamp
            and (message.sender_id or "") == (other.sender_id or "")
            and message.role == other.role
        )

    @staticmethod
    def _format_experience_entry(
        entry_type: str,
        message: Message,
        content: str,
    ) -> List[str]:
        if entry_type == "observation":
            return [MessageHandler._format_context_event_header(message), content]

        header = MessageHandler._format_transcript_message_header(message)
        lines = [header, content]
        image_count = MessageHandler._count_message_images(message)
        if image_count:
            noun = "image" if image_count == 1 else "images"
            lines.append(f"[Attached {noun}: {image_count}]")
        attachment_count = MessageHandler._count_message_attachments(message)
        if attachment_count and attachment_count != image_count:
            noun = "file" if attachment_count == 1 else "files"
            lines.append(f"[Attached {noun}: {attachment_count}]")
        return lines

    @staticmethod
    def _storage_cursor(message: Message) -> Optional[int]:
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        raw = metadata.get(AgentConfig.MESSAGE_STORAGE_CURSOR_KEY)
        try:
            cursor = int(raw)
        except (TypeError, ValueError):
            return None
        return cursor if cursor > 0 else None

    @staticmethod
    def _budget_context_events(
        messages: List[Message],
        max_events: int,
        covers_through_cursor: int = 0,
    ) -> tuple[List[tuple[Message, str]], int]:
        if not messages:
            return [], 0

        selected, omitted_count = MessageHandler._budget_by_coverage(
            messages,
            max_keep=max(1, int(max_events or AgentConfig.MAX_CONTEXT_EVENTS)),
            covers_through_cursor=covers_through_cursor,
        )
        return [
            (
                msg,
                msg.content.strip() or "[Empty observation]",
            )
            for msg in selected
        ], omitted_count

    @staticmethod
    def _format_context_event_header(message: Message) -> str:
        header = f"[ambient context][timestamp={MessageHandler._format_transcript_timestamp(message)}]"
        if message.sender_id or str((message.metadata or {}).get("sender_name") or "").strip():
            speaker = MessageHandler._sanitize_marker_field(
                MessageHandler._format_transcript_speaker(message)
            )
            if speaker:
                header += f"[from={speaker}]"
        if message.channel:
            header += f"[channel={message.channel}]"
        if message.room_name:
            safe_room = MessageHandler._sanitize_marker_field(message.room_name)
            header += f"[room={safe_room}]"
        return header

    @staticmethod
    def _format_transcript_message_header(message: Message) -> str:
        timestamp = MessageHandler._format_transcript_timestamp(message)
        if is_scheduled_work(message.metadata):
            header = f"[scheduled task][timestamp={timestamp}]"
            target = MessageHandler._sanitize_marker_field(message.sender_id)
            if target:
                header += f"[for={target}]"
        else:
            speaker = MessageHandler._sanitize_marker_field(
                MessageHandler._format_transcript_speaker(message)
            )
            header = f"[speaker={speaker}][timestamp={timestamp}]"
        if message.channel:
            header += f"[channel={message.channel}]"
        if message.room_name:
            safe_room = MessageHandler._sanitize_marker_field(message.room_name)
            header += f"[room={safe_room}]"
        return header

    @staticmethod
    def _sanitize_marker_field(value: Optional[str]) -> str:
        return str(value or "").strip().replace("\n", " ").replace("]", "")

    @staticmethod
    def _format_transcript_timestamp(message: Message) -> str:
        return datetime.fromtimestamp(message.timestamp).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _budget_by_coverage(
        messages: List[Message],
        *,
        max_keep: int,
        covers_through_cursor: int = 0,
    ) -> tuple[List[Message], int]:
        """Keep uncovered messages; never drop rows the summary has not covered.

        When storage cursors are present:
        - ``cursor > covers`` is always kept (may temporarily exceed ``max_keep``
          while roll_slack defers compaction)
        - ``cursor <= covers`` is omitted from the raw prompt (owned by summary)

        Without cursors, keep the newest ``max_keep`` rows, except when nothing
        is covered yet — then keep everything to avoid the pre-summary gap.
        """
        if not messages:
            return [], 0

        keep_limit = max(1, int(max_keep or 1))
        covers = max(0, int(covers_through_cursor or 0))
        cursors = [MessageHandler._storage_cursor(message) for message in messages]
        if any(cursor is not None for cursor in cursors):
            selected = [
                message
                for message, cursor in zip(messages, cursors)
                if cursor is None or cursor > covers
            ]
            # Hard safety valve only; normal path relies on the compactor.
            return selected, 0

        # No storage cursors (tests / legacy): classic newest-N budget.
        omitted_count = max(0, len(messages) - keep_limit)
        return messages[-keep_limit:], omitted_count

    @staticmethod
    def _budget_transcript_entries(
        messages: List[Message],
        max_messages: int,
        covers_through_cursor: int = 0,
    ) -> tuple[List[tuple[Message, str]], int]:
        if not messages:
            return [], 0

        selected, omitted_count = MessageHandler._budget_by_coverage(
            messages,
            max_keep=max(1, int(max_messages or AgentConfig.DEFAULT_RECENT_MESSAGES)),
            covers_through_cursor=covers_through_cursor,
        )
        return [
            (msg, msg.content.strip() or "[Empty message]")
            for msg in selected
        ], omitted_count

    @staticmethod
    def _count_message_images(message: Message) -> int:
        metadata_images = message.metadata.get("images") if isinstance(message.metadata, dict) else None
        if isinstance(metadata_images, list):
            return len(metadata_images)
        if not message.images:
            return 0
        return len(message.images)

    @staticmethod
    def _count_message_attachments(message: Message) -> int:
        metadata_attachments = message.metadata.get(ATTACHMENT_METADATA_KEY) if isinstance(message.metadata, dict) else None
        return len(metadata_attachments) if isinstance(metadata_attachments, list) else 0

    @staticmethod
    def _append_attachment_manifest(user_message: str, attachments: List[Dict[str, Any]]) -> str:
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
    def _attachments_from_image_metadata(image_metadata: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        attachments: List[Dict[str, Any]] = []
        for metadata in image_metadata:
            workspace_path = str(metadata.get("workspace_path") or "").strip()
            blob_url = str(metadata.get("blob_url") or "").strip()
            if not workspace_path and not blob_url:
                continue
            file_name = str(metadata.get("original_name") or Path(workspace_path).name or "image").strip()
            attachments.append({
                "kind": "image",
                "path": workspace_path,
                "blob_url": blob_url,
                "mime_type": str(metadata.get("mime_type") or "image/png").strip(),
                "file_name": file_name,
                "size_bytes": metadata.get("size_bytes"),
            })
        return dedupe_attachments(attachments)

    @staticmethod
    def _format_transcript_speaker(message: Message) -> str:
        if message.role == RoleType.ASSISTANT:
            return "ME"
        from ...components.memory import format_speaker_label

        metadata = message.metadata or {}
        return format_speaker_label(
            message.sender_id or "",
            str(metadata.get("sender_name") or ""),
            channel=message.channel or "",
        ) or message.role.value

    @staticmethod
    def _speaker_display_name(
        messages: List[Message],
        user_id: str,
        current_message: Optional[Message] = None,
    ) -> str:
        source = current_message
        if source is None or (source.sender_id or "") != user_id:
            for message in reversed(messages or []):
                if message.role == RoleType.USER and (message.sender_id or "") == user_id:
                    source = message
                    break
        if source is None:
            return ""
        return str((source.metadata or {}).get("sender_name") or "")

    @staticmethod
    def _speaker_label_for(
        messages: List[Message],
        user_id: str,
        current_message: Optional[Message] = None,
    ) -> str:
        """Cognitive speaker tag for this person on the current channel."""
        from ...components.memory import format_speaker_label

        channel = ""
        if current_message is not None:
            channel = current_message.channel or ""
        elif messages:
            channel = messages[-1].channel or ""
        return format_speaker_label(
            user_id,
            MessageHandler._speaker_display_name(messages, user_id, current_message),
            channel=channel,
        ) or user_id

    @staticmethod
    def _speaker_address_for(
        messages: List[Message],
        user_id: str,
        current_message: Optional[Message] = None,
    ) -> str:
        """Name to address in the current-task prompt — display name, else id."""
        from ...components.memory import speaker_address_name

        return speaker_address_name(
            user_id,
            MessageHandler._speaker_display_name(messages, user_id, current_message),
        ) or user_id

    @staticmethod
    def _latest_current_user_message(
        messages: List[Message],
        current_user_id: str,
    ) -> Optional[Message]:
        if not messages:
            return None
        message = messages[-1]
        if message.role == RoleType.USER and message.sender_id == current_user_id:
            return message
        return None

    @staticmethod
    def _current_message_images(
        message: Optional[Message],
        current_user_id: str,
        *,
        workspace_dir: Optional[Union[str, Path]] = None,
    ) -> List[str]:
        if message is None or message.role != RoleType.USER or message.sender_id != current_user_id:
            return []
        if not message.images:
            return []
        return [
            image_source
            for image in message.images
            if (image_source := MessageHandler._model_image_source(image, workspace_dir=workspace_dir))
        ]

    @staticmethod
    def _model_image_source(image: Any, *, workspace_dir: Optional[Union[str, Path]] = None) -> str:
        source = extract_source(str(getattr(image, "source", None) or image or "")).strip()
        if not source:
            return ""

        source_type = classify_source(source)
        if source_type == ImageSourceType.URL:
            return source
        if source_type == ImageSourceType.DATA_URI:
            image_bytes, mime_type = data_uri_to_bytes(source)
            return bytes_to_data_uri(image_bytes, mime_type)

        if source_type == ImageSourceType.WORKSPACE_BLOB:
            if workspace_dir is None:
                raise ValueError("Workspace image blob input requires a configured workspace directory")
            image_path = resolve_workspace_blob_path(source, workspace_dir)
            if image_path is None:
                raise ValueError("Invalid workspace image blob URL")
        else:
            image_path = MessageHandler._resolve_local_image_path(source, workspace_dir=workspace_dir)

        return workspace_image_data_uri(image_path)

    def _merge_image_sources(
        self,
        user_message: str,
        image_source: Optional[Union[str, List[str]]],
    ) -> List[str]:
        sources: List[str] = []
        if image_source:
            sources.extend(image_source if isinstance(image_source, list) else [image_source])
        sources.extend(extract_image_urls_from_text(user_message))
        if self.workspace_dir is not None:
            root = Path(self.workspace_dir).expanduser().resolve()
            for path in extract_workspace_image_paths_from_text(user_message):
                resolved = resolve_workspace_file_path(path, root)
                if resolved is None or not workspace_path_is_image(resolved):
                    continue
                sources.append(workspace_blob_url(resolved.relative_to(root).as_posix()))

        merged: List[str] = []
        seen: set[str] = set()
        for source in sources:
            normalized = self._canonical_image_source(str(source or ""))
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
        return merged

    def _canonical_image_source(self, source: str) -> str:
        normalized = extract_source(source).strip()
        if not normalized or self.workspace_dir is None:
            return normalized
        resolved = resolve_workspace_file_path(normalized, self.workspace_dir)
        if resolved is None or not resolved.is_file():
            return normalized
        root = Path(self.workspace_dir).expanduser().resolve()
        return workspace_blob_url(resolved.relative_to(root).as_posix())

    def _prepare_message_images(self, image_sources: List[str]) -> tuple[List[str], List[Dict[str, Any]]]:
        if not image_sources:
            return [], []
        if len(image_sources) > MAX_IMAGES_PER_MESSAGE:
            raise ValueError(f"At most {MAX_IMAGES_PER_MESSAGE} images are allowed per message")

        normalized_sources: List[str] = []
        image_metadata: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for source in image_sources:
            normalized_source, metadata = self._normalize_message_image_source(source)
            if normalized_source in seen:
                continue
            seen.add(normalized_source)
            normalized_sources.append(normalized_source)
            image_metadata.append(metadata)
        return normalized_sources, image_metadata

    def _preview_image_metadata(self, image_sources: List[str]) -> List[Dict[str, Any]]:
        metadata_items: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for source in image_sources[:MAX_IMAGES_PER_MESSAGE]:
            try:
                normalized_source, metadata = self._normalize_message_image_source(source)
            except ValueError as exc:
                logger.warning("Skipping invalid image preview metadata: %s", exc)
                continue
            if normalized_source in seen:
                continue
            seen.add(normalized_source)
            metadata_items.append(metadata)
        return metadata_items

    def _normalize_message_image_source(self, source: str) -> tuple[str, Dict[str, Any]]:
        raw_source = extract_source(str(source or "")).strip()
        if not raw_source:
            raise ValueError("Image source cannot be empty")

        source_type = classify_source(raw_source)
        if source_type == ImageSourceType.URL:
            return raw_source, self._clean_image_metadata({
                "external_url": raw_source,
                "mime_type": self._mime_type_from_source(raw_source),
            })

        if source_type == ImageSourceType.DATA_URI:
            image_bytes, mime_type = data_uri_to_bytes(raw_source)
            if self.workspace_dir is not None:
                metadata = save_image_bytes_to_workspace(image_bytes, mime_type, self.workspace_dir)
                return str(metadata["blob_url"]), self._clean_image_metadata(metadata)
            return bytes_to_data_uri(image_bytes, mime_type), self._clean_image_metadata({
                "mime_type": mime_type,
                "size_bytes": len(image_bytes),
            })

        if source_type == ImageSourceType.WORKSPACE_BLOB:
            relative_path = workspace_blob_relative_path(raw_source)
            if not relative_path:
                raise ValueError("Invalid workspace image blob URL")
            metadata: Dict[str, Any] = {
                "workspace_path": relative_path,
                "blob_url": workspace_blob_url(relative_path),
                "mime_type": self._mime_type_from_source(relative_path),
            }
            if self.workspace_dir is not None:
                image_path = resolve_workspace_blob_path(raw_source, self.workspace_dir)
                if image_path is None:
                    raise ValueError("Invalid workspace image blob URL")
                image_bytes, mime_type = read_image_file_bytes(image_path, allowed_mime_types=None)
                metadata.update({
                    "mime_type": mime_type,
                    "size_bytes": len(image_bytes),
                    "original_name": image_path.name,
                })
            return str(metadata["blob_url"]), self._clean_image_metadata(metadata)

        image_path = self._resolve_local_image_path(raw_source, workspace_dir=self.workspace_dir)
        image_bytes, mime_type = read_image_file_bytes(image_path)
        if self.workspace_dir is not None:
            root = self.workspace_dir
            resolved_path = image_path.resolve()
            if resolved_path.is_relative_to(root):
                relative_path = resolved_path.relative_to(root).as_posix()
                metadata = {
                    "workspace_path": relative_path,
                    "blob_url": workspace_blob_url(relative_path),
                    "mime_type": mime_type,
                    "size_bytes": len(image_bytes),
                    "original_name": resolved_path.name,
                }
                return str(metadata["blob_url"]), self._clean_image_metadata(metadata)
            metadata = save_image_bytes_to_workspace(
                image_bytes,
                mime_type,
                self.workspace_dir,
                original_name=image_path.name,
            )
            return str(metadata["blob_url"]), self._clean_image_metadata(metadata)
        return bytes_to_data_uri(image_bytes, mime_type), self._clean_image_metadata({
            "mime_type": mime_type,
            "size_bytes": len(image_bytes),
            "original_name": image_path.name,
        })

    @staticmethod
    def _resolve_local_image_path(
        source: str,
        *,
        workspace_dir: Optional[Union[str, Path]] = None,
    ) -> Path:
        raw_path = Path(source).expanduser()
        if workspace_dir is not None and not raw_path.is_absolute():
            root = Path(workspace_dir).expanduser().resolve()
            workspace_path = (root / source).resolve()
            if workspace_path.is_relative_to(root) and workspace_path.exists():
                return workspace_path
        return raw_path.resolve()

    @staticmethod
    def _clean_image_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in metadata.items() if value not in (None, "")}

    @staticmethod
    def _mime_type_from_source(source: str) -> str:
        image_format = infer_format(source)
        if image_format == "jpeg":
            return "image/jpeg"
        if image_format == "webp":
            return "image/webp"
        if image_format == "gif":
            return "image/gif"
        return "image/png"

    def build_instruction_messages(
        self,
        tool_names: Optional[List[str]] = None,
        skills_catalog: str = "",
        supports_vision: bool = True,
        workspace_context: str = "",
        channel_instructions: str = "",
        operator_policy: str = "",
        is_subconscious: bool = False,
    ) -> list[dict]:
        """Build static named system layers for the model input.

        When *is_subconscious* is True a ``current_mode`` layer is injected
        so the model knows it cannot execute tasks or use tools this turn.
        When *supports_vision* is False a ``capability_limits`` layer is
        injected instead of appending a notice onto core rules.
        """
        messages, _entries = self.build_instruction_messages_with_manifest(
            tool_names=tool_names,
            skills_catalog=skills_catalog,
            supports_vision=supports_vision,
            workspace_context=workspace_context,
            channel_instructions=channel_instructions,
            operator_policy=operator_policy,
            is_subconscious=is_subconscious,
        )
        return messages

    def build_instruction_messages_with_manifest(
        self,
        tool_names: Optional[List[str]] = None,
        skills_catalog: str = "",
        supports_vision: bool = True,
        workspace_context: str = "",
        channel_instructions: str = "",
        operator_policy: str = "",
        is_subconscious: bool = False,
    ) -> tuple[list[dict], list[ManifestEntry]]:
        ctx = PromptAssembleContext(
            system_prompt=self.system_prompt,
            operator_policy=operator_policy or getattr(self, "operator_policy", "") or "",
            tool_names=list(tool_names or []),
            skills_catalog=skills_catalog,
            workspace_context=workspace_context,
            channel_instructions=channel_instructions,
            supports_vision=supports_vision,
            is_subconscious=is_subconscious,
        )
        return self.prompt_registry.assemble_with_manifest(KIND_INSTRUCTIONS, ctx)

    def build_decision_messages(self) -> list[dict]:
        """Named system layers for a participation decision request."""
        ctx = PromptAssembleContext(system_prompt=self.system_prompt)
        return self.prompt_registry.assemble(KIND_DECISION, ctx)

    @staticmethod
    def sanitize_input_messages(input_messages: list) -> list:
        """Remove leading tool result messages, which are invalid without a prior assistant tool call."""
        while input_messages and (
            input_messages[0].get("type") == "function_call_output"
            or input_messages[0].get("role") == "tool"
        ):
            input_messages.pop(0)
        return input_messages
