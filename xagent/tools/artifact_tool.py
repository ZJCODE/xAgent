"""Workspace artifact attachment tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from xagent.schemas.attachment import attachment_markdown, workspace_attachment_from_path
from xagent.utils.image_utils import workspace_blob_relative_path
from xagent.utils.tool_decorator import function_tool


ARTIFACT_ATTACHMENT_TYPE = "artifact_attachment"


def create_attach_artifact_tool(*, workspace_dir: str):
    """Create a tool that registers a workspace file for user delivery."""
    workspace_root = Path(workspace_dir).expanduser().resolve()

    @function_tool(
        name="attach_artifact",
        description=(
            "Attach an existing workspace file so it is delivered to the user as an artifact. "
            "Use this after creating or modifying a file that the user asked you to send, show, "
            "return, or share. Do not only mention the file path in prose when the user expects "
            "the file itself."
        ),
        param_descriptions={
            "path": (
                "Workspace-relative path, workspace blob URL, or absolute path inside the configured workspace."
            ),
            "caption": "Optional short caption to send before the artifact.",
        },
    )
    async def attach_artifact(path: str, caption: Optional[str] = None) -> dict:
        resolved_path = _resolve_workspace_artifact_path(path, workspace_root)
        if resolved_path is None:
            return _artifact_error("Artifact path must stay inside the workspace")
        if not resolved_path.exists():
            return _artifact_error("Artifact path does not exist")
        if resolved_path.is_dir():
            return _artifact_error("Artifact path must point to a file, not a directory")
        if not resolved_path.is_file():
            return _artifact_error("Artifact path must point to a regular file")

        artifact = workspace_attachment_from_path(
            resolved_path,
            workspace_root,
            caption=str(caption or "").strip(),
        )
        return {
            "status": "ok",
            "type": ARTIFACT_ATTACHMENT_TYPE,
            "artifact": artifact,
        }

    return attach_artifact


def is_artifact_attachment_result(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("status") == "ok"
        and result.get("type") == ARTIFACT_ATTACHMENT_TYPE
        and isinstance(result.get("artifact"), dict)
        and bool(result["artifact"].get("path") or result["artifact"].get("blob_url"))
    )


def artifact_attachment_markdown(result: dict) -> str:
    return attachment_markdown(result.get("artifact") or {})


def artifact_attachment_description(tool_name: str, result: dict) -> str:
    artifact = result.get("artifact") or {}
    path = str(artifact.get("path") or "").strip()
    caption = str(artifact.get("caption") or "").strip()
    description = f"[Artifact attached by tool `{tool_name}` and displayed to user."
    if path:
        description += f" Saved path: {path}."
    if caption:
        description += f" Caption: {caption}."
    return description + "]"


def artifact_attachments(result: dict) -> list[dict]:
    artifact = result.get("artifact")
    return [dict(artifact)] if isinstance(artifact, dict) else []


def _resolve_workspace_artifact_path(source: str, workspace_root: Path) -> Optional[Path]:
    source = str(source or "").strip().strip("<>")
    if not source:
        return None

    relative_path = workspace_blob_relative_path(source)
    if relative_path:
        candidate = (workspace_root / relative_path).resolve()
    else:
        parsed = urlparse(source)
        if parsed.scheme and parsed.scheme != "file":
            return None
        raw_path = unquote(parsed.path if parsed.scheme == "file" else source)
        candidate_path = Path(raw_path).expanduser()
        candidate = candidate_path.resolve() if candidate_path.is_absolute() else (workspace_root / raw_path).resolve()

    if not candidate.is_relative_to(workspace_root):
        return None
    return candidate


def _artifact_error(message: str) -> dict:
    return {
        "status": "error",
        "type": ARTIFACT_ATTACHMENT_TYPE,
        "message": message,
    }
