"""Local, agent-data-only service used by the admin/monitoring routes.

``AdminService`` owns the parts of the runtime that are pure local filesystem
access (memory, workspace, skills, tasks, messages, identity, config) with no
dependency on a live chat transport. It is shared by:

* ``AgentHTTPServer`` (the api channel), which composes it alongside the
  chat-only ``ApiChannelAdapter``.
* The built-in web client, which can construct one ``AdminService`` per
  registered agent so its Memory/Message/Workspace/Skills/Tasks/Agent tabs
  work without any api channel running.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import HTTPException

from ..base import BaseAgentConfig, BaseAgentRunner
from .files import WorkspaceFileService
from ...components.skills import SkillsStorageLocal
from ...core.agent import Agent


class AdminService(BaseAgentRunner):
    """Local admin/monitoring data access for one agent's config directory."""

    def __init__(
        self,
        config_dir: Optional[str] = None,
        agent: Optional[Agent] = None,
    ):
        if agent is not None:
            self.agent = agent
            config_dir_path = Path(
                getattr(agent, "config_dir", None) or config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR
            ).expanduser().resolve()
            self.config_dir = config_dir_path
            self.config_path = config_dir_path / BaseAgentConfig.CONFIG_FILENAME
            self.identity_path = config_dir_path / BaseAgentConfig.IDENTITY_FILENAME
            try:
                self.config = self._load_config(self.config_path)
            except Exception:
                self.config = {}
            try:
                self.identity = self._load_identity(self.identity_path)
            except Exception:
                self.identity = getattr(agent, "system_prompt", "") or getattr(agent, "identity", "")
            self.message_storage = self.agent.message_storage
            self.skills_storage = getattr(self.agent, "skills_storage", None)
            self._temporary_runtime = None
            runtime_root = getattr(self.agent, "workspace", None) or str(config_dir_path)
            if runtime_root is None:
                self._temporary_runtime = tempfile.TemporaryDirectory(prefix="xagent-runtime-")
                runtime_root = self._temporary_runtime.name
            self.workspace = Path(runtime_root).expanduser().resolve()
            self.workspace_dir = Path(
                getattr(self.agent, "workspace_dir", self.workspace / BaseAgentConfig.WORKSPACE_DIRNAME)
            ).expanduser().resolve()
            self.tasks_dir = self.workspace / BaseAgentConfig.TASKS_DIRNAME
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
            self.logger = logging.getLogger(self.__class__.__name__)
        else:
            super().__init__(config_dir=config_dir)

    def _get_memory_root(self) -> Path:
        memory = self.agent.markdown_memory
        memory_root = getattr(memory, "root", None)
        if memory_root is None:
            raise HTTPException(status_code=500, detail="Memory storage path is unavailable")
        return Path(memory_root).expanduser().resolve()

    def _get_workspace_root(self) -> Path:
        workspace_dir = getattr(self, "workspace_dir", None)
        if workspace_dir is None:
            workspace_dir = getattr(self.agent, "workspace_dir", None)
        if workspace_dir is None:
            runtime_root = getattr(self, "workspace", None)
            if runtime_root is not None:
                workspace_dir = Path(runtime_root) / BaseAgentConfig.WORKSPACE_DIRNAME
        if workspace_dir is None:
            memory_root = self._get_memory_root()
            workspace_dir = memory_root.parent / BaseAgentConfig.WORKSPACE_DIRNAME
        root = Path(workspace_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _workspace_files(self) -> WorkspaceFileService:
        return WorkspaceFileService(self._get_workspace_root())

    def _get_skills_root(self) -> Path:
        skills_storage = getattr(self, "skills_storage", None)
        if skills_storage is not None:
            root = getattr(skills_storage, "root", None)
            if root is not None:
                return Path(root).expanduser().resolve()
        runtime_root = getattr(self, "workspace", None)
        if runtime_root is not None:
            skills_root = Path(runtime_root) / BaseAgentConfig.SKILLS_DIRNAME
        else:
            memory_root = self._get_memory_root()
            skills_root = memory_root.parent / BaseAgentConfig.SKILLS_DIRNAME
        root = Path(skills_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _get_skills_storage(self) -> SkillsStorageLocal:
        skills_storage = getattr(self, "skills_storage", None)
        if isinstance(skills_storage, SkillsStorageLocal):
            return skills_storage
        storage = SkillsStorageLocal(self._get_skills_root())
        self.skills_storage = storage
        if not hasattr(self.agent, "skills_storage"):
            try:
                self.agent.skills_storage = storage
            except Exception:
                pass
        return storage

    @staticmethod
    def _raise_skills_http_error(exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail=f"Skills error: {str(exc)}") from exc

    @staticmethod
    def _memory_scope_roots(memory_dir: Path) -> List[Path]:
        return [memory_dir / scope for scope in ("daily", "weekly", "monthly", "yearly", "relationships")]

    def _get_identity_path(self) -> Path:
        identity_path = getattr(self, "identity_path", None)
        if identity_path is None:
            raise HTTPException(status_code=500, detail="Identity file path is unavailable")
        return Path(identity_path).expanduser().resolve()

    def _get_agent_identity(self) -> str:
        identity = getattr(self.agent, "identity", None)
        if identity is None:
            identity = getattr(self.agent, "system_prompt", "")
        return identity or ""

    def _set_agent_identity(self, identity: str) -> None:
        if hasattr(self.agent, "set_identity"):
            self.agent.set_identity(identity)
        else:
            self.agent.system_prompt = identity
            message_handler = getattr(self.agent, "message_handler", None)
            if message_handler is not None:
                message_handler.system_prompt = identity
        self.identity = identity
