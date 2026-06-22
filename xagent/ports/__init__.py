"""Runtime-facing component protocols."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from ..domain.message_records import MessageBatch, StoredMessage
from ..domain.skills import SkillMetadata
from ..domain import Message


class MessageStore(Protocol):
    """Ordered short-term message store for one agent stream."""

    async def add_messages(self, messages: MessageBatch, **kwargs: Any) -> None:
        ...

    async def get_messages(self, count: int = 20, offset: int = 0) -> List[Message]:
        ...

    async def get_stored_messages(self, count: int = 20, offset: int = 0) -> List[StoredMessage]:
        ...

    async def clear_messages(self) -> None:
        ...

    async def pop_message(self) -> Optional[Message]:
        ...

    async def get_message_count(self) -> int:
        ...

    async def get_latest_message_id(self) -> int:
        ...

    async def get_messages_by_id_range(
        self,
        start_exclusive: int = 0,
        end_inclusive: Optional[int] = None,
    ) -> List[StoredMessage]:
        ...

    async def search_messages(
        self,
        query: str,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        max_results: int = 500,
    ) -> str:
        ...

    def get_stream_info(self) -> Dict[str, str]:
        ...


class MemoryStore(Protocol):
    """Long-term memory file store."""

    root: Path

    async def append_daily(self, content: str, target_date: Any = None) -> Path:
        ...

    async def read_file(self, path: Path) -> str:
        ...

    async def read_recent_dailies(self, days: int = 3) -> List[tuple[str, str]]:
        ...

    async def write_summary(self, path: Path, content: str) -> Path:
        ...

    async def search_keyword(self, query: str, scope: str = "all", context_lines: int = 3) -> str:
        ...

    async def search_date_range(self, start: str, end: Optional[str] = None) -> str:
        ...

    async def list_files(self, scope: str = "all") -> List[str]:
        ...


class SkillStore(Protocol):
    """Filesystem-backed Agent Skills catalog."""

    def list_skills(self, *, include_disabled: bool = True, include_invalid: bool = True) -> List[SkillMetadata]:
        ...

    def get_skill(self, name: str, *, include_disabled: bool = False) -> Optional[SkillMetadata]:
        ...

    def catalog_text(self, *, max_chars: int) -> str:
        ...

    def read_skill_file(self, skill_name: str, file_path: str = "SKILL.md") -> Dict[str, Any]:
        ...
