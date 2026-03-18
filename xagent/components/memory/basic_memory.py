import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base_memory import MemoryStorageBase
from .config.memory_config import (
    EXPLICIT_MEMORY_PATTERNS,
    MEMORY_DUPLICATE_SCORE_THRESHOLD,
    MEMORY_EXTRACTION_INTERVAL_SECONDS,
    MEMORY_FORCE_EXTRACTION_MULTIPLIER,
    MEMORY_MAX_BATCH_MESSAGES,
    MEMORY_REPLACEMENT_MIN_LENGTH_DELTA,
    MEMORY_RETRIEVAL_MIN_SCORE,
    MEMORY_RETRIEVAL_OVERSCAN,
)
from .helper.llm_service import MemoryLLMService
from .vector.base_vector_store import VectorDoc


@dataclass
class StreamMemoryState:
    """Extraction cursor and schedule state for a single agent memory stream."""

    last_processed_message_count: int = 0
    last_extracted_at: float = 0.0


class MemoryStorageBasic(MemoryStorageBase):
    """
    Simplified long-term memory pipeline shared by local and cloud backends.

    Design principles:
    - Recent global transcript remains the primary source of truth
    - Long-term memory is compressed support context
    - Extraction is delayed and batched instead of running every turn
    """

    def __init__(
        self,
        memory_threshold: int = 10,
        message_storage=None,
        vector_store=None,
        memory_interval_seconds: int = MEMORY_EXTRACTION_INTERVAL_SECONDS,
        max_batch_messages: int = MEMORY_MAX_BATCH_MESSAGES,
    ):
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.llm_service = MemoryLLMService()
        self.vector_store = vector_store
        self.message_storage = message_storage
        self.memory_threshold = max(1, memory_threshold)
        self.memory_interval_seconds = max(0, memory_interval_seconds)
        self.max_batch_messages = max(1, max_batch_messages)
        self.force_extraction_threshold = max(
            self.memory_threshold + 1,
            self.memory_threshold * MEMORY_FORCE_EXTRACTION_MULTIPLIER,
        )
        self._stream_states: Dict[str, StreamMemoryState] = {}
        self._stream_locks: Dict[str, asyncio.Lock] = {}
        self._memory_key_locks: Dict[str, asyncio.Lock] = {}
        self._explicit_memory_patterns = [
            re.compile(pattern, re.IGNORECASE) for pattern in EXPLICIT_MEMORY_PATTERNS
        ]

    async def add(
        self,
        memory_key: str,
        messages: List[Dict[str, Any]],
    ):
        """Persist delayed long-term memories from unread global transcript batches."""
        if not messages:
            return

        async with self._stream_lock(memory_key):
            state = self._stream_states.setdefault(memory_key, StreamMemoryState())
            current_message_count = await self._get_stream_message_count(messages)

            if current_message_count < state.last_processed_message_count:
                self.logger.info(
                    "Message stream for %s shrank from %d to %d messages; resetting memory cursor.",
                    memory_key,
                    state.last_processed_message_count,
                    current_message_count,
                )
                state.last_processed_message_count = 0
                state.last_extracted_at = 0.0

            unread_count = max(0, current_message_count - state.last_processed_message_count)
            if unread_count <= 0:
                return

            explicit_trigger = self._contains_explicit_memory_intent(
                self._extract_user_messages(messages)
            )
            if not explicit_trigger and not self._should_extract(state, unread_count):
                return

            try:
                serialised, processed_message_count = await self._collect_unprocessed_messages(
                    last_processed_message_count=state.last_processed_message_count,
                    current_message_count=current_message_count,
                    fallback_messages=messages,
                    max_batch_messages=self.max_batch_messages,
                )
                if processed_message_count <= state.last_processed_message_count:
                    return

                content = self._format_messages_for_storage(serialised)
                if content:
                    await self.store(memory_key, content)

                state.last_processed_message_count = processed_message_count
                state.last_extracted_at = time.time()
            except Exception as exc:
                self.logger.error(
                    "Failed to store memories for %s from message stream: %s",
                    memory_key,
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
                    self._create_base_metadata(
                        memory_key=memory_key,
                        memory_type=memory_piece.type.value,
                    )
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
        """Retrieve memories with one semantic query and lightweight post-filtering."""
        try:
            vector_docs = await self.vector_store.query(
                query_texts=[query],
                n_results=max(limit * MEMORY_RETRIEVAL_OVERSCAN, limit),
                meta_filter={"memory_key": memory_key},
            )
        except Exception as exc:
            self.logger.error("Failed to retrieve memories for %s: %s", memory_key, exc)
            return []

        filtered_docs: List[VectorDoc] = []
        for vector_doc in vector_docs:
            if vector_doc.score is not None and vector_doc.score < MEMORY_RETRIEVAL_MIN_SCORE:
                continue
            filtered_docs.append(vector_doc)

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
        self._stream_states.pop(memory_key, None)

    async def delete(self, memory_ids: List[str]):
        await self.vector_store.delete(memory_ids)

    async def _get_stream_message_count(
        self,
        fallback_messages: Sequence[Dict[str, Any]],
    ) -> int:
        if self.message_storage is None:
            return len(fallback_messages)
        return await self.message_storage.get_message_count()

    async def _collect_unprocessed_messages(
        self,
        last_processed_message_count: int,
        current_message_count: int,
        fallback_messages: Sequence[Dict[str, Any]],
        max_batch_messages: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        if self.message_storage is None:
            unread_messages = list(fallback_messages)[:max_batch_messages]
            processed_count = min(len(fallback_messages), max_batch_messages)
            return self._serialise_memory_messages(unread_messages), processed_count

        if current_message_count <= 0:
            return [], 0

        stored_messages = await self.message_storage.get_messages(count=current_message_count)
        start = max(0, min(last_processed_message_count, len(stored_messages)))
        unread_messages = stored_messages[start:start + max_batch_messages]
        processed_count = start + len(unread_messages)
        return self._serialise_memory_messages(unread_messages), processed_count

    def _serialise_memory_messages(self, messages: Sequence[Any]) -> List[Dict[str, Any]]:
        serialised: List[Dict[str, Any]] = []
        for message in messages:
            if not self._is_memory_candidate_message(message):
                continue

            if hasattr(message, "to_dict"):
                item = message.to_dict()
            else:
                item = dict(message)

            timestamp = self._message_value(message, "timestamp")
            if timestamp is not None:
                item["timestamp"] = timestamp
            serialised.append(item)
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

    def _should_extract(
        self,
        state: StreamMemoryState,
        unread_count: int,
    ) -> bool:
        if unread_count >= self.force_extraction_threshold:
            return True
        if unread_count < self.memory_threshold:
            return False
        if state.last_extracted_at <= 0:
            return True
        return (time.time() - state.last_extracted_at) >= self.memory_interval_seconds

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

            prefix_parts: List[str] = []
            timestamp = self._format_timestamp(msg.get("timestamp"))
            if timestamp:
                prefix_parts.append(f"[{timestamp}]")
            prefix_parts.append(role)
            if sender_id:
                prefix_parts.append(str(sender_id))

            lines.append(f"{' '.join(prefix_parts)}: {content}")
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
        if normalized == "semantic":
            return 0
        if normalized == "social":
            return 1
        if normalized == "episodic":
            return 2
        if normalized == "self":
            return 3
        return 4

    def _create_base_metadata(
        self,
        memory_key: str,
        memory_type: str,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "memory_key": memory_key,
            "created_at": now.isoformat(),
            "created_timestamp": now.timestamp(),
            "memory_type": memory_type,
            "type": memory_type,
        }

    @staticmethod
    def _format_timestamp(timestamp: Any) -> str:
        if timestamp is None:
            return ""
        try:
            dt = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return ""
        return dt.strftime("%Y-%m-%d %H:%M:%SZ")

    def _stream_lock(self, memory_key: str) -> asyncio.Lock:
        return self._stream_locks.setdefault(memory_key, asyncio.Lock())

    def _memory_key_lock(self, memory_key: str) -> asyncio.Lock:
        return self._memory_key_locks.setdefault(memory_key, asyncio.Lock())
