"""Service layer for the unified memory system."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .experience_store import ExperienceMemoryStore, MemoryStatus


@dataclass
class MemoryExtractorService:
    """Coordinates model-backed extraction into candidate memory items.

    The first implementation keeps extraction orchestration in ``MemoryHandler``
    and exposes this class as the stable service boundary for future richer
    extractors.
    """

    store: ExperienceMemoryStore
    llm_service: Any


@dataclass
class MemoryReconciler:
    """Reconcile candidate memories into the canonical store."""

    store: ExperienceMemoryStore

    async def activate(self, memory_id: int, *, reason: str = "activated") -> dict[str, Any]:
        item = await self.store.get_memory_item(memory_id, include_evidence=False)
        if item is None:
            return {"status": "not_found", "memory_id": memory_id}
        await self.store.correct_memory(
            memory_id=memory_id,
            correction=item["content"],
            reason=reason,
            actor="reconciler",
        )
        return {"status": "ok", "memory_id": memory_id}


@dataclass
class MemoryRetriever:
    """High-level memory retrieval facade."""

    store: ExperienceMemoryStore

    async def recall(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return await self.store.recall_memory(query=query, **kwargs)

    async def search_history(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return await self.store.search_history(query=query, **kwargs)


@dataclass
class MemorySummarizer:
    """Summary service boundary for periodic and scoped reflections."""

    store: ExperienceMemoryStore
    llm_service: Any


@dataclass
class MemoryRetentionService:
    """Apply simple expiration policies to active memories."""

    store: ExperienceMemoryStore

    async def archive_expired(self, *, now: Optional[float] = None, limit: int = 200) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        result = await self.store.query_sql(
            (
                "SELECT id FROM memory_items "
                "WHERE status = 'active' AND valid_until IS NOT NULL AND valid_until < "
                f"{timestamp} LIMIT {max(1, min(int(limit), 200))}"
            ),
            max_rows=limit,
        )
        archived: list[int] = []
        for row in result.get("rows", []):
            memory_id = int(row["id"])
            await self.store.forget_memory(
                memory_id=memory_id,
                mode="archive",
                reason="retention expiry",
                actor="retention",
            )
            archived.append(memory_id)
        return {"status": "ok", "archived": archived, "count": len(archived)}
