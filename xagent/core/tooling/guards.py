"""Tool execution guards: policy runs around tool bodies, not inside them."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Protocol


class ToolDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class ToolCallContext:
    name: str
    args: dict
    workspace_dir: Optional[Path] = None


@dataclass(frozen=True)
class ToolGuardResult:
    decision: ToolDecision
    reason: str = ""

    @classmethod
    def allow(cls) -> "ToolGuardResult":
        return cls(ToolDecision.ALLOW)

    @classmethod
    def deny(cls, reason: str) -> "ToolGuardResult":
        return cls(ToolDecision.DENY, reason=reason)

    @classmethod
    def ask(cls, reason: str) -> "ToolGuardResult":
        return cls(ToolDecision.ASK, reason=reason)


class WorkspaceEscapeError(ValueError):
    """Raised when a shell cwd would leave the agent workspace."""


def resolve_workspace_cwd(
    working_directory: Optional[str],
    workspace_root: str | Path,
) -> str:
    """Resolve a cwd and reject paths that escape ``workspace_root``."""
    root = Path(workspace_root).expanduser().resolve()
    if not working_directory or not str(working_directory).strip():
        return str(root)
    candidate = Path(working_directory).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspaceEscapeError(
            f"working_directory {str(resolved)!r} is outside the agent workspace {str(root)!r}"
        ) from exc
    return str(resolved)


class ToolGuard(Protocol):
    async def pre_execute(self, ctx: ToolCallContext) -> ToolGuardResult:
        ...

    async def post_execute(self, ctx: ToolCallContext, result: Any) -> Any:
        ...


class AllowAllGuard:
    async def pre_execute(self, ctx: ToolCallContext) -> ToolGuardResult:
        return ToolGuardResult.allow()

    async def post_execute(self, ctx: ToolCallContext, result: Any) -> Any:
        return result


class WorkspaceShellGuard:
    """Force ``run_command`` cwd to stay inside the agent workspace."""

    TOOL_NAME = "run_command"

    def __init__(self, workspace_dir: str | Path) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser().resolve()

    async def pre_execute(self, ctx: ToolCallContext) -> ToolGuardResult:
        if ctx.name != self.TOOL_NAME:
            return ToolGuardResult.allow()
        working_directory = ctx.args.get("working_directory")
        try:
            resolve_workspace_cwd(working_directory, self.workspace_dir)
        except WorkspaceEscapeError as exc:
            return ToolGuardResult.deny(str(exc))
        return ToolGuardResult.allow()

    async def post_execute(self, ctx: ToolCallContext, result: Any) -> Any:
        return result
