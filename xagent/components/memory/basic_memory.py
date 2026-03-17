import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base_memory import MemoryStorageBase
from .config.memory_config import (
    EXPLICIT_MEMORY_PATTERNS,
    MEMORY_DUPLICATE_SCORE_THRESHOLD,
    MEMORY_REPLACEMENT_MIN_LENGTH_DELTA,
    MEMORY_RETRIEVAL_MIN_SCORE,
)
from .helper.llm_service import MemoryLLMService
from .vector.base_vector_store import VectorDoc


@dataclass
class ConversationMemoryState:
    """In-memory extraction state for a single conversation transcript."""

    pending_user_turns: int = 0
    last_processed_message_count: int = 0


class MemoryStorageBasic(MemoryStorageBase):
    """
    Minimal long-term memory pipeline shared by local and cloud backends.

    The design is intentionally narrow:
    - Count user turns per conversation
    - Trigger extraction on threshold or explicit "remember this" intent
    - Read only the unread transcript segment for that conversation
    - Extract memory pieces once and store them directly
    - Retrieve with a single vector query using the original user query
    """

    def __init__(
        self,
        memory_threshold: int = 10,
        message_storage=None,
        vector_store=None,
    ):
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.llm_service = MemoryLLMService()
        self.vector_store = vector_store
        self.message_storage = message_storage
        self.memory_threshold = memory_threshold
        self._conversation_states: Dict[str, ConversationMemoryState] = {}
        self._conversation_locks: Dict[str, asyncio.Lock] = {}
        self._memory_key_locks: Dict[str, asyncio.Lock] = {}
        self._explicit_memory_patterns = [
            re.compile(pattern, re.IGNORECASE) for pattern in EXPLICIT_MEMORY_PATTERNS
        ]

    async def add(
        self,
        memory_key: str,
        conversation_id: str,
        messages: List[Dict[str, Any]],
    ):
        """Track user turns and persist long-term memories from unread transcript segments."""
        if not messages:
            return

        async with self._conversation_lock(conversation_id):
            state = self._conversation_states.setdefault(
                conversation_id,
                ConversationMemoryState(),
            )

            current_message_count = await self._get_conversation_message_count(
                conversation_id,
                messages,
            )
            if current_message_count < state.last_processed_message_count:
                self.logger.info(
                    "Conversation %s shrank from %d to %d messages; resetting memory cursor.",
                    conversation_id,
                    state.last_processed_message_count,
                    current_message_count,
                )
                state.pending_user_turns = 0
                state.last_processed_message_count = 0

            user_messages = self._extract_user_messages(messages)
            if not user_messages:
                return

            state.pending_user_turns += len(user_messages)
            explicit_trigger = self._contains_explicit_memory_intent(user_messages)
            if not explicit_trigger and state.pending_user_turns < self.memory_threshold:
                return

            try:
                serialised, processed_message_count = await self._collect_unprocessed_messages(
                    conversation_id=conversation_id,
                    last_processed_message_count=state.last_processed_message_count,
                    current_message_count=current_message_count,
                    fallback_messages=messages,
                )
                content = self._format_messages_for_storage(serialised)
                if content:
                    await self.store(memory_key, content)

                state.last_processed_message_count = processed_message_count
                state.pending_user_turns = 0
            except Exception as exc:
                self.logger.error(
                    "Failed to store memories for %s from conversation %s: %s",
                    memory_key,
                    conversation_id,
                    exc,
                )

    async def store(
        self,
        memory_key: str,
        content: str,
    ) -> str:
        """Extract and store memory pieces with lightweight near-duplicate suppression."""
        async with self._memory_key_lock(memory_key):
            extracted = await self.llm_service.extract_memories_from_content(content)
            if not extracted.memories:
                return ""

            ids: List[str] = []
            documents: List[str] = []
            metadatas: List[Dict[str, Any]] = []

            for memory_piece in extracted.memories:
                document = self._normalise_memory_content(memory_piece.content)
                if not document:
                    continue

                document_id = str(uuid.uuid4())
                duplicate = await self._find_near_duplicate(
                    memory_key=memory_key,
                    memory_type=memory_piece.type.value,
                    document=document,
                )
                if duplicate is not None:
                    if not self._should_replace_existing(duplicate.document, document):
                        continue
                    document_id = duplicate.id

                ids.append(document_id)
                documents.append(document)
                metadatas.append(
                    self._create_base_metadata(memory_key, memory_piece.type.value)
                )

            if not ids:
                self.logger.info("Skipped duplicate-only memory write for %s", memory_key)
                return ""

            await self.vector_store.upsert(ids=ids, documents=documents, metadatas=metadatas)
            self.logger.info("Stored %d memory pieces for %s", len(ids), memory_key)
            return ids[0] if len(ids) == 1 else str(ids)

    async def retrieve(
        self,
        memory_key: str,
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve memories with one semantic query and a small relevance gate."""
        try:
            vector_docs = await self.vector_store.query(
                query_texts=[query],
                n_results=limit,
                meta_filter={"memory_key": memory_key},
            )
        except Exception as exc:
            self.logger.error("Failed to retrieve memories for %s: %s", memory_key, exc)
            return []

        filtered_docs = [
            vector_doc
            for vector_doc in vector_docs
            if vector_doc.score is None or vector_doc.score >= MEMORY_RETRIEVAL_MIN_SCORE
        ]
        filtered_docs.sort(
            key=lambda doc: (
                self._memory_type_priority(doc.metadata.get("memory_type")),
                -(doc.score if doc.score is not None else 1.0),
                -float(doc.metadata.get("created_timestamp", 0.0)),
            )
        )

        results: List[Dict[str, Any]] = []
        for vector_doc in filtered_docs[:limit]:
            metadata = {
                key: value
                for key, value in vector_doc.metadata.items()
                if key not in {"created_timestamp", "memory_key"}
            }
            results.append({
                "content": vector_doc.document,
                "metadata": metadata,
            })
        return results

    async def clear(self, memory_key: str) -> None:
        await self.vector_store.delete_by_filter({"memory_key": memory_key})

    async def delete(self, memory_ids: List[str]):
        await self.vector_store.delete(memory_ids)

    async def _get_conversation_message_count(
        self,
        conversation_id: str,
        fallback_messages: Sequence[Dict[str, Any]],
    ) -> int:
        if self.message_storage is None:
            return len(fallback_messages)
        return await self.message_storage.get_message_count(conversation_id)

    async def _collect_unprocessed_messages(
        self,
        conversation_id: str,
        last_processed_message_count: int,
        current_message_count: int,
        fallback_messages: Sequence[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], int]:
        if self.message_storage is None:
            unread_messages = list(fallback_messages)
            return self._serialise_memory_messages(unread_messages), len(unread_messages)

        if current_message_count <= 0:
            return [], 0

        stored_messages = await self.message_storage.get_messages(
            conversation_id,
            count=current_message_count,
        )
        start = max(0, min(last_processed_message_count, len(stored_messages)))
        unread_messages = stored_messages[start:]
        return self._serialise_memory_messages(unread_messages), len(stored_messages)

    def _serialise_memory_messages(self, messages: Sequence[Any]) -> List[Dict[str, Any]]:
        serialised: List[Dict[str, Any]] = []
        for message in messages:
            if not self._is_memory_candidate_message(message):
                continue
            if hasattr(message, "to_dict"):
                serialised.append(message.to_dict())
            else:
                serialised.append(dict(message))
        return serialised

    def _extract_user_messages(self, messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            message
            for message in messages
            if str(message.get("role", "")).lower() == "user" and bool(message.get("content"))
        ]

    def _contains_explicit_memory_intent(self, messages: Sequence[Dict[str, Any]]) -> bool:
        for message in messages:
            content = str(message.get("content", ""))
            if any(pattern.search(content) for pattern in self._explicit_memory_patterns):
                return True
        return False

    def _is_memory_candidate_message(self, message: Any) -> bool:
        role = self._message_value(message, "role")
        return role in {"user", "assistant"}

    @staticmethod
    def _message_value(message: Any, field: str) -> Any:
        if hasattr(message, field):
            value = getattr(message, field)
            return getattr(value, "value", value)
        if isinstance(message, dict):
            return message.get(field)
        return None

    def _format_messages_for_storage(self, messages: List[Dict[str, Any]]) -> str:
        lines: List[str] = []

        for msg in messages:
            role = str(msg.get("role", "unknown")).title()
            sender_id = msg.get("sender_id")
            content = str(msg.get("content", "")).strip()

            if not content:
                continue

            prefix = f"{role} {sender_id}" if sender_id else role
            lines.append(f"{prefix}: {content}")

        return "\n\n".join(lines)

    async def _find_near_duplicate(
        self,
        memory_key: str,
        memory_type: str,
        document: str,
    ) -> Optional[VectorDoc]:
        try:
            candidates = await self.vector_store.query(
                query_texts=[document],
                n_results=3,
                meta_filter={"memory_key": memory_key},
            )
        except Exception as exc:
            self.logger.warning(
                "Duplicate check failed for %s; storing memory without suppression: %s",
                memory_key,
                exc,
            )
            return None

        document_norm = self._normalise_memory_content(document)
        for candidate in candidates:
            candidate_type = candidate.metadata.get("memory_type") or candidate.metadata.get("type")
            if candidate_type != memory_type:
                continue
            candidate_norm = self._normalise_memory_content(candidate.document)
            if candidate_norm == document_norm:
                return candidate
            if candidate.score is not None and candidate.score >= MEMORY_DUPLICATE_SCORE_THRESHOLD:
                return candidate
        return None

    def _should_replace_existing(self, existing_document: str, new_document: str) -> bool:
        existing_norm = self._normalise_memory_content(existing_document)
        new_norm = self._normalise_memory_content(new_document)

        if not existing_norm or existing_norm == new_norm:
            return False

        length_delta = len(new_norm) - len(existing_norm)
        return length_delta >= MEMORY_REPLACEMENT_MIN_LENGTH_DELTA and existing_norm in new_norm

    @staticmethod
    def _normalise_memory_content(content: str) -> str:
        return " ".join(str(content).split())

    @staticmethod
    def _memory_type_priority(memory_type: Optional[str]) -> int:
        normalized = str(memory_type or "").lower()
        if normalized == "profile":
            return 0
        if normalized == "episodic":
            return 1
        return 2

    def _create_base_metadata(self, memory_key: str, memory_type: str) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "memory_key": memory_key,
            "created_at": now.isoformat(),
            "created_timestamp": now.timestamp(),
            "memory_type": memory_type,
            "type": memory_type,
        }

    def _conversation_lock(self, conversation_id: str) -> asyncio.Lock:
        return self._conversation_locks.setdefault(conversation_id, asyncio.Lock())

    def _memory_key_lock(self, memory_key: str) -> asyncio.Lock:
        return self._memory_key_locks.setdefault(memory_key, asyncio.Lock())
