"""Safe rooted filesystem helpers for the HTTP workspace API."""

from __future__ import annotations

import mimetypes
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from ..utils.image_utils import workspace_blob_url


class WorkspaceFileService:
    """Expose safe read/write/search operations rooted at one workspace directory."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, relative_path: str = "") -> Path:
        requested = (self.root / (relative_path or "")).expanduser().resolve()
        if not requested.is_relative_to(self.root):
            raise HTTPException(status_code=403, detail="Access denied")
        return requested

    def resolve_upload_path(self, raw_target: str, filename: str) -> Path:
        target_is_directory = raw_target.endswith("/")
        target_relative = raw_target.strip("/")
        if not target_relative:
            return self.resolve_path(filename)

        target = self.resolve_path(target_relative)
        requested = target / filename if target_is_directory or target.is_dir() else target
        requested = requested.resolve()
        if not requested.is_relative_to(self.root):
            raise HTTPException(status_code=403, detail="Access denied")
        return requested

    def metadata(self, path: Path) -> Dict[str, Any]:
        resolved = path.resolve()
        stat = resolved.stat()
        is_dir = resolved.is_dir()
        mime_type, _ = mimetypes.guess_type(resolved.name)
        return {
            "name": resolved.name,
            "path": str(resolved.relative_to(self.root)),
            "type": "dir" if is_dir else "file",
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "mime_type": mime_type or "application/octet-stream",
            "binary": False if is_dir else self._is_binary_file(resolved),
        }

    def scan_tree(self, directory: Optional[Path] = None) -> List[Dict[str, Any]]:
        current = directory or self.root
        entries: List[Dict[str, Any]] = []
        try:
            children = sorted(current.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower()))
        except (OSError, PermissionError):
            return entries

        for child in children:
            resolved = self._safe_child(child)
            if resolved is None:
                continue
            try:
                item = self.metadata(resolved)
            except OSError:
                continue
            if item["type"] == "dir" and not child.is_symlink():
                item["children"] = self.scan_tree(resolved)
            entries.append(item)
        return entries

    def read(self, relative_path: str, *, text_limit: int) -> Dict[str, Any]:
        requested = self.resolve_path(relative_path)
        if not requested.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        metadata = self.metadata(requested)
        if metadata["binary"]:
            return {**metadata, "content": "", "text": False, "blob_url": workspace_blob_url(relative_path)}

        content = self._read_text_file(requested, text_limit)
        return {**metadata, "content": content, "text": True, "blob_url": workspace_blob_url(relative_path)}

    def search(self, query: str, *, limit: int, text_limit: int) -> List[Dict[str, Any]]:
        needle = query.strip().lower()
        results: List[Dict[str, Any]] = []

        for file_path in sorted(self.root.rglob("*")):
            if len(results) >= limit:
                break

            resolved = self._safe_child(file_path)
            if resolved is None or not resolved.is_file():
                continue

            relative_path = str(resolved.relative_to(self.root))
            match_kind: List[str] = []
            snippet = ""

            if needle in resolved.name.lower() or needle in relative_path.lower():
                match_kind.append("filename")

            is_binary = self._is_binary_file(resolved)
            if not is_binary and resolved.stat().st_size <= text_limit:
                try:
                    content = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    content = ""
                lower_content = content.lower()
                content_index = lower_content.find(needle)
                if content_index != -1:
                    match_kind.append("content")
                    start = max(0, content_index - 80)
                    end = min(len(content), content_index + len(query) + 120)
                    snippet = content[start:end].replace("\n", " ").strip()

            if match_kind:
                results.append({
                    **self.metadata(resolved),
                    "matched_in": match_kind,
                    "snippet": snippet,
                })

        return results

    def clear(self) -> int:
        deleted_count = 0
        try:
            for child in self.root.iterdir():
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    resolved = self._safe_child(child)
                    if resolved is None:
                        continue
                    shutil.rmtree(resolved)
                else:
                    child.unlink(missing_ok=True)
                deleted_count += 1
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to clear workspace: {str(exc)}") from exc
        return deleted_count

    def write_text(self, relative_path: str, *, content: str, create_parents: bool) -> Dict[str, Any]:
        requested = self.resolve_path(relative_path)
        if requested.exists() and requested.is_dir():
            raise HTTPException(status_code=400, detail="Path is a directory")
        if create_parents:
            requested.parent.mkdir(parents=True, exist_ok=True)
        elif not requested.parent.is_dir():
            raise HTTPException(status_code=404, detail="Parent directory not found")
        requested.write_text(content, encoding="utf-8")
        return self.metadata(requested)

    def delete(self, relative_path: str, *, recursive: bool) -> Dict[str, Any]:
        requested = self.resolve_path(relative_path)
        if requested == self.root:
            raise HTTPException(status_code=400, detail="Cannot delete workspace root")
        if not requested.exists():
            raise HTTPException(status_code=404, detail="Path not found")

        metadata = self.metadata(requested)
        if requested.is_dir():
            if recursive:
                shutil.rmtree(requested)
            else:
                requested.rmdir()
        else:
            requested.unlink()
        return metadata

    def _safe_child(self, path: Path) -> Optional[Path]:
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(self.root):
            return None
        return resolved

    @staticmethod
    def _is_binary_file(path: Path) -> bool:
        try:
            chunk = path.read_bytes()[:4096]
        except OSError:
            return True
        if b"\0" in chunk:
            return True
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return True
        return False

    @staticmethod
    def _read_text_file(path: Path, limit: int) -> str:
        if path.stat().st_size > limit:
            raise HTTPException(status_code=413, detail="File is too large to read as text")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="File is not UTF-8 text") from exc