import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..config import AgentConfig
from ..prompts import PromptAssembler
from ...components import MessageStorage
from ...schemas import Message, RoleType, MessageType
from ...schemas.attachment import (
    ATTACHMENT_METADATA_KEY,
    attachment_image_sources,
    attachment_manifest_markdown,
    dedupe_attachments,
)
from ...utils.image_utils import (
    MAX_IMAGES_PER_MESSAGE,
    ImageSourceType,
    bytes_to_data_uri,
    classify_source,
    data_uri_to_bytes,
    extract_image_urls_from_text,
    extract_source,
    infer_format,
    read_image_file_bytes,
    resolve_workspace_blob_path,
    save_image_bytes_to_workspace,
    workspace_blob_relative_path,
    workspace_blob_url,
)

logger = logging.getLogger(__name__)


class MessageHandler:
    """Handles message storage, retrieval, sanitization, and system prompt building."""

    def __init__(
        self,
        message_storage: MessageStorage,
        system_prompt: str = "",
        workspace_dir: Optional[Union[str, Path]] = None,
    ):
        self.message_storage = message_storage
        self.system_prompt = system_prompt
        self.workspace_dir = Path(workspace_dir).expanduser().resolve() if workspace_dir is not None else None

    async def store_user_message(
        self,
        user_message: str,
        user_id: str,
        image_source: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        room_name: Optional[str] = None,
        source: Optional[str] = None,
        recipient_id: Optional[str] = None,
        runtime_event_id: Optional[str] = None,
    ) -> Message:
        """Store a user message, auto-detecting embedded image URLs and attachments."""
        normalized_attachments = dedupe_attachments(list(attachments or []))
        message_content = self._append_attachment_manifest(user_message, normalized_attachments)
        image_sources = self._merge_image_sources(message_content, image_source)
        for source in attachment_image_sources(normalized_attachments):
            if source not in image_sources:
                image_sources.append(source)
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
        if source:
            msg.source = source
        if normalized_attachments:
            msg.metadata[ATTACHMENT_METADATA_KEY] = normalized_attachments
        if image_metadata:
            msg.metadata["images"] = image_metadata
        if runtime_event_id:
            return await self.message_storage.add_message_once(
                msg,
                f"event:{runtime_event_id}:input",
            )
        await self.message_storage.add_messages(msg)
        return msg

    async def store_model_reply(
        self,
        reply_text: str,
        sender_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        room_name: Optional[str] = None,
        source: Optional[str] = None,
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
        if source:
            model_msg.source = source
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
        role: RoleType = RoleType.ENVIRONMENT,
        event_source: Optional[str] = None,
        recipient_id: Optional[str] = None,
        runtime_event_id: Optional[str] = None,
    ) -> Message:
        """Store a non-direct observation from the agent's environment."""
        event_msg = Message.create_context_event(
            content=context,
            source=source,
            event_type=event_type,
            metadata=metadata,
            role=role,
        )
        if recipient_id:
            event_msg.recipient_id = recipient_id
        if room_name:
            event_msg.room_name = room_name
        if event_source:
            event_msg.source = event_source
        if runtime_event_id:
            return await self.message_storage.add_message_once(
                event_msg,
                f"event:{runtime_event_id}:observation",
            )
        await self.message_storage.add_messages(event_msg)
        return event_msg

    async def get_recent_messages(
        self,
        max_history: int,
    ) -> List[Message]:
        return await self.message_storage.get_messages(max_history)

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
        context_events: Optional[List[Message]] = None,
        current_time: Optional[str] = None,
        max_messages: int = AgentConfig.DEFAULT_MAX_HISTORY,
        max_context_events: int = AgentConfig.MAX_CONTEXT_EVENTS,
        include_images: bool = True,
        workspace_dir: Optional[Union[str, Path]] = None,
        current_message: Optional[Message] = None,
        channel_instructions: str = "",
        task_mode: str = "reply",
    ) -> list[dict]:
        """Build the per-turn model input context as named message layers."""
        conversation_messages = MessageHandler.filter_conversation_messages(messages)
        if current_message is not None:
            for index in range(len(conversation_messages) - 1, -1, -1):
                candidate = conversation_messages[index]
                if MessageHandler._same_event(candidate, current_message):
                    del conversation_messages[index]
                    break
        observation_messages = (
            MessageHandler.filter_context_events(messages)
            if context_events is None
            else MessageHandler.filter_context_events(context_events)
        )
        budgeted_entries, omitted_count = MessageHandler._budget_transcript_entries(
            conversation_messages,
            max_messages=max_messages,
        )
        budgeted_observations, omitted_observation_count = MessageHandler._budget_context_events(
            observation_messages,
            max_events=max_context_events,
        )
        experience_entries = MessageHandler._merge_experience_entries(
            budgeted_entries,
            budgeted_observations,
        )

        context_messages: list[dict] = []
        if memory_context.strip():
            context_messages.append({
                "role": RoleType.USER.value,
                "name": AgentConfig.RECENT_MEMORY_NAME,
                "content": MessageHandler._wrap_untrusted_context(
                    AgentConfig.RECENT_MEMORY_NAME,
                    memory_context,
                ),
            })

        context_messages.append({
            "role": RoleType.USER.value,
            "name": AgentConfig.RECENT_EXPERIENCE_NAME,
            "content": MessageHandler._build_recent_experience_context(
                experience_entries=experience_entries,
                omitted_messages=omitted_count,
                omitted_observations=omitted_observation_count,
            ),
        })

        resolved_current_time = (
            current_time
            or datetime.now().strftime("%Y-%m-%d %H:%M")
        )
        if task_mode == "subconscious_json":
            current_task_text = PromptAssembler.subconscious_task(resolved_current_time)
        else:
            current_task_text = PromptAssembler.current_task(
                speaker_id=current_user_id,
                current_time=resolved_current_time,
                channel_instructions=channel_instructions,
            )
        current_task_message = {
            "role": RoleType.USER.value,
            "name": AgentConfig.CURRENT_TASK_NAME,
            "content": current_task_text,
        }

        current_images: List[str] = []
        if current_message is not None:
            current_event_content = MessageHandler._current_event_context(
                current_message,
                current_user_id=current_user_id,
            )
            current_event_message: dict[str, Any] = {
                "role": RoleType.USER.value,
                "name": AgentConfig.CURRENT_EVENT_NAME,
                "content": current_event_content,
            }
            context_messages.append(current_event_message)

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
        if current_images:
            target_message = (
                context_messages[-1]
                if current_message is not None
                else current_task_message
            )
            text = str(target_message["content"])
            content = [{"type": "text", "text": text}]
            content.extend(
                {"type": "image_url", "image_url": {"url": image_source}}
                for image_source in current_images
            )
            target_message["content"] = content

        context_messages.append(current_task_message)
        return context_messages

    @staticmethod
    def _same_event(left: Message, right: Message) -> bool:
        return (
            left.role == right.role
            and left.sender_id == right.sender_id
            and left.content == right.content
            and abs(float(left.timestamp) - float(right.timestamp)) < 0.001
        )

    @staticmethod
    def _current_event_context(
        message: Message,
        *,
        current_user_id: str,
    ) -> str:
        speaker = message.sender_id or current_user_id
        header = (
            f"[speaker={speaker}]"
            f"[timestamp={MessageHandler._format_transcript_timestamp(message)}]"
        )
        if message.source:
            header += f"[source={message.source}]"
        if message.room_name:
            room = message.room_name.replace("\n", " ").replace("]", "")
            header += f"[room={room}]"
        content = message.content.strip() or "[Empty event]"
        return (
            "<current_event>\n"
            f"{header}\n{content}\n"
            "</current_event>"
        )

    @staticmethod
    def _build_recent_experience_context(
        experience_entries: List[tuple[str, Message, str]],
        omitted_messages: int,
        omitted_observations: int,
    ) -> str:
        lines: list[str] = []
        if omitted_messages or omitted_observations:
            lines.append(
                MessageHandler._format_omitted_experience_note(
                    omitted_messages=omitted_messages,
                    omitted_observations=omitted_observations,
                )
            )
            lines.append("")

        for entry_type, msg, content in experience_entries:
            lines.extend(MessageHandler._format_experience_entry(entry_type, msg, content))
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
    def _format_experience_entry(
        entry_type: str,
        message: Message,
        content: str,
    ) -> List[str]:
        if entry_type == "observation":
            return [MessageHandler._format_context_event_header(message), content]

        lines = [
            MessageHandler._format_transcript_message_header(message),
            content,
        ]
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
    def _budget_context_events(
        messages: List[Message],
        max_events: int,
    ) -> tuple[List[tuple[Message, str]], int]:
        if not messages:
            return [], 0

        event_limit = max(1, int(max_events or AgentConfig.MAX_CONTEXT_EVENTS))
        omitted_count = max(0, len(messages) - event_limit)
        selected = messages[-event_limit:]
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
        if message.source:
            header += f"[source={message.source}]"
        if message.room_name:
            safe_room = message.room_name.replace("\n", " ").replace("]", "")
            header += f"[room={safe_room}]"
        return header

    @staticmethod
    def _format_transcript_message_header(message: Message) -> str:
        speaker = MessageHandler._format_transcript_speaker(message)
        timestamp = MessageHandler._format_transcript_timestamp(message)
        header = f"[speaker={speaker}][timestamp={timestamp}]"
        if message.source:
            header += f"[source={message.source}]"
        if message.room_name:
            safe_room = message.room_name.replace("\n", " ").replace("]", "")
            header += f"[room={safe_room}]"
        return header

    @staticmethod
    def _format_transcript_timestamp(message: Message) -> str:
        return datetime.fromtimestamp(message.timestamp).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _budget_transcript_entries(
        messages: List[Message],
        max_messages: int,
    ) -> tuple[List[tuple[Message, str]], int]:
        if not messages:
            return [], 0

        message_limit = max(1, int(max_messages or AgentConfig.DEFAULT_MAX_HISTORY))
        omitted_count = max(0, len(messages) - message_limit)
        candidates = messages[-message_limit:]
        return [
            (msg, msg.content.strip() or "[Empty message]")
            for msg in candidates
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
        return message.sender_id or message.role.value

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

        image_bytes, mime_type = read_image_file_bytes(image_path, allowed_mime_types=None)
        return bytes_to_data_uri(image_bytes, mime_type)

    def _merge_image_sources(
        self,
        user_message: str,
        image_source: Optional[Union[str, List[str]]],
    ) -> List[str]:
        sources: List[str] = []
        if image_source:
            sources.extend(image_source if isinstance(image_source, list) else [image_source])
        sources.extend(extract_image_urls_from_text(user_message))

        merged: List[str] = []
        seen: set[str] = set()
        for source in sources:
            normalized = extract_source(str(source or "")).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
        return merged

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
        is_subconscious: bool = False,
        memory_recent_days: int = AgentConfig.MEMORY_RECENT_DAYS,
    ) -> list[dict]:
        # Tool behavior is described exactly once, by the tool schemas.
        del tool_names, memory_recent_days
        return PromptAssembler.instruction_messages(
            identity=self.system_prompt,
            skills_catalog=skills_catalog,
            workspace_context=workspace_context,
            supports_vision=supports_vision,
            is_subconscious=is_subconscious,
        )

    @staticmethod
    def sanitize_input_messages(input_messages: list) -> list:
        """Remove leading tool result messages, which are invalid without a prior assistant tool call."""
        while input_messages and (
            input_messages[0].get("type") == "function_call_output"
            or input_messages[0].get("role") == "tool"
        ):
            input_messages.pop(0)
        return input_messages
