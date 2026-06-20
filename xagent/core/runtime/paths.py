"""Runtime directory layout for one agent instance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import AgentConfig


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved runtime paths owned by one agent instance."""

    root: Path

    @classmethod
    def from_root(cls, root: str | Path | None = None) -> "RuntimePaths":
        raw_root = root if root is not None else AgentConfig.DEFAULT_RUNTIME_ROOT
        return cls(root=Path(raw_root).expanduser().resolve())

    @property
    def memory_dir(self) -> Path:
        return self.root / AgentConfig.MEMORY_DIRNAME

    @property
    def messages_dir(self) -> Path:
        return self.root / AgentConfig.MESSAGE_DIRNAME

    @property
    def messages_db(self) -> Path:
        return self.messages_dir / AgentConfig.MESSAGE_DB_FILENAME

    @property
    def workspace_dir(self) -> Path:
        return self.root / AgentConfig.WORKSPACE_DIRNAME

    @property
    def skills_dir(self) -> Path:
        return self.root / AgentConfig.SKILLS_DIRNAME

    @property
    def tasks_dir(self) -> Path:
        return self.root / AgentConfig.TASKS_DIRNAME

    def ensure_directories(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.messages_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
