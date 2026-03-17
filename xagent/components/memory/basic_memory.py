import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from .base_memory import MemoryStorageBase
from .helper.llm_service import MemoryLLMService


class MemoryStorageBasic(MemoryStorageBase):
    """
    Minimal long-term memory pipeline shared by local and cloud backends.

    The design is intentionally narrow:
    - Count recent conversation turns per memory owner
    - When the threshold is reached, read recent conversation history
    - Extract memory pieces once and store them directly
    - Retrieve with a single vector query using the original query
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
        self._message_counter: Dict[str, int] = {}

    async def add(
        self,
        memory_key: str,
        conversation_id: str,
        messages: List[Dict[str, Any]],
    ):
        """Track conversation turns and persist long-term memories at a fixed threshold."""
        if not messages:
            return

        self._message_counter[memory_key] = self._message_counter.get(memory_key, 0) + len(messages)
        message_count = self._message_counter[memory_key]

        if message_count < self.memory_threshold:
            return

        try:
            if self.message_storage is not None:
                recent_messages = await self.message_storage.get_messages(
                    conversation_id,
                    count=max(message_count, self.memory_threshold),
                )
                serialised = [
                    message.to_dict()
                    for message in recent_messages
                    if message.role.value in ("user", "assistant")
                ]
            else:
                serialised = messages

            content = self._format_messages_for_storage(serialised)
            if content:
                await self.store(memory_key, content)
        except Exception as exc:
            self.logger.error("Failed to store memories for %s: %s", memory_key, exc)
        finally:
            self._message_counter[memory_key] = 0

    async def store(
        self,
        memory_key: str,
        content: str,
    ) -> str:
        """Extract and store memory pieces directly without fusion or dedup rewrite."""
        extracted = await self.llm_service.extract_memories_from_content(content)
        if not extracted.memories:
            return ""

        ids: List[str] = []
        documents: List[str] = []
        metadatas: List[Dict[str, Any]] = []

        for memory_piece in extracted.memories:
            ids.append(str(uuid.uuid4()))
            documents.append(memory_piece.content)
            metadatas.append(self._create_base_metadata(memory_key, memory_piece.type.value))

        await self.vector_store.upsert(ids=ids, documents=documents, metadatas=metadatas)
        self.logger.info("Stored %d memory pieces for %s", len(ids), memory_key)
        return ids[0] if len(ids) == 1 else str(ids)

    async def retrieve(
        self,
        memory_key: str,
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve memories with a single vector query against the original user query."""
        try:
            vector_docs = await self.vector_store.query(
                query_texts=[query],
                n_results=limit,
                meta_filter={"memory_key": memory_key},
            )
        except Exception as exc:
            self.logger.error("Failed to retrieve memories for %s: %s", memory_key, exc)
            return []

        results: List[Dict[str, Any]] = []
        for vector_doc in vector_docs:
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
        self._message_counter.pop(memory_key, None)

    async def delete(self, memory_ids: List[str]):
        await self.vector_store.delete(memory_ids)

    def _format_messages_for_storage(self, messages: List[Dict[str, Any]]) -> str:
        lines: List[str] = []

        for msg in messages:
            role = msg.get("role", "unknown")
            sender_id = msg.get("sender_id")
            content = msg.get("content", "")

            if not content:
                continue

            prefix = role.title()
            if sender_id:
                prefix = f"{prefix} {sender_id}"
            lines.append(f"{prefix}: {content}")

        return "\n\n".join(lines)

    def _create_base_metadata(self, memory_key: str, memory_type: str) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "memory_key": memory_key,
            "created_at": now.isoformat(),
            "created_timestamp": now.timestamp(),
            "memory_type": memory_type,
        }
