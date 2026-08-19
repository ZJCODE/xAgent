"""Open a stored workspace image onto the model's vision channel."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xagent.utils.image_utils import (
    detect_image_mime,
    resolve_workspace_file_path,
    workspace_blob_url,
    workspace_path_is_image,
)
from xagent.utils.tool_decorator import function_tool


SEE_IMAGE_TYPE = "see_image"


def create_see_image_tool(*, workspace_dir: str):
    """Create a tool that requests vision input for a workspace image."""
    workspace_root = Path(workspace_dir).expanduser().resolve()

    @function_tool(
        name="see_image",
        description=(
            "Open an existing workspace image so you can see it on this turn. "
            "Use when a stored file should be looked at again. "
            "This does not describe the image and does not send it to the user; "
            "use attach_artifact to deliver a file."
        ),
        param_descriptions={
            "path": (
                "Workspace-relative path, workspace blob URL, or absolute path inside the workspace."
            ),
        },
    )
    async def see_image(path: str) -> dict:
        resolved_path = resolve_workspace_file_path(path, workspace_root)
        if resolved_path is None:
            return _see_image_error("Image path must stay inside the workspace")
        if not resolved_path.exists():
            return _see_image_error("Image path does not exist")
        if resolved_path.is_dir():
            return _see_image_error("Image path must point to a file, not a directory")
        if not resolved_path.is_file():
            return _see_image_error("Image path must point to a regular file")
        if not workspace_path_is_image(resolved_path):
            return _see_image_error("Path is not an image")

        relative_path = resolved_path.relative_to(workspace_root).as_posix()
        image_bytes = resolved_path.read_bytes()
        mime_type = detect_image_mime(image_bytes) or "image/png"
        return {
            "status": "ok",
            "type": SEE_IMAGE_TYPE,
            "path": relative_path,
            "blob_url": workspace_blob_url(relative_path),
            "mime_type": mime_type,
            "size_bytes": len(image_bytes),
        }

    return see_image


def is_see_image_result(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("status") == "ok"
        and result.get("type") == SEE_IMAGE_TYPE
        and bool(result.get("path"))
    )


def see_image_observation(result: dict) -> str:
    path = str(result.get("path") or "").strip()
    mime_type = str(result.get("mime_type") or "").strip()
    size_bytes = result.get("size_bytes")
    details = [part for part in (mime_type, f"{size_bytes} bytes" if isinstance(size_bytes, int) else "") if part]
    suffix = f" ({', '.join(details)})" if details else ""
    return (
        f"Image is now visible to you: {path}{suffix}. "
        "Look at the image in this turn's input; this text is not a description of contents."
    )


def _see_image_error(message: str) -> dict:
    return {
        "status": "error",
        "type": SEE_IMAGE_TYPE,
        "message": message,
    }
