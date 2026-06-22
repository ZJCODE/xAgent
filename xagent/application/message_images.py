"""Image and attachment normalization for persisted messages."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..domain import Message, RoleType
from ..domain.attachments import (
    attachment_image_sources,
    dedupe_attachments,
)
from ..infrastructure.media.images import (
    MAX_IMAGES_PER_MESSAGE,
    ImageSourceType,
    bytes_to_data_uri,
    classify_source,
    data_uri_to_bytes,
    extract_image_urls_from_text,
    extract_source,
    infer_format,
    read_image_file_bytes,
    resolve_workspace_blob_path,
    save_image_bytes_to_workspace,
    workspace_blob_relative_path,
    workspace_blob_url,
)

logger = logging.getLogger(__name__)


class MessageImageNormalizer:
    """Normalize user-facing image inputs into model-safe sources and metadata."""

    def __init__(self, workspace_dir: Optional[Union[str, Path]] = None) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser().resolve() if workspace_dir is not None else None

    def merge_sources(
        self,
        message_text: str,
        image_source: Optional[Union[str, List[str]]],
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        sources: List[str] = []
        if image_source:
            sources.extend(image_source if isinstance(image_source, list) else [image_source])
        sources.extend(extract_image_urls_from_text(message_text))
        for source in attachment_image_sources(attachments or []):
            sources.append(source)

        merged: List[str] = []
        seen: set[str] = set()
        for source in sources:
            normalized = extract_source(str(source or "")).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
        return merged

    def prepare_message_images(self, image_sources: List[str]) -> tuple[List[str], List[Dict[str, Any]]]:
        if not image_sources:
            return [], []
        if len(image_sources) > MAX_IMAGES_PER_MESSAGE:
            raise ValueError(f"At most {MAX_IMAGES_PER_MESSAGE} images are allowed per message")

        normalized_sources: List[str] = []
        image_metadata: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for source in image_sources:
            normalized_source, metadata = self.normalize_source(source)
            if normalized_source in seen:
                continue
            seen.add(normalized_source)
            normalized_sources.append(normalized_source)
            image_metadata.append(metadata)
        return normalized_sources, image_metadata

    def preview_metadata(self, image_sources: List[str]) -> List[Dict[str, Any]]:
        metadata_items: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for source in image_sources[:MAX_IMAGES_PER_MESSAGE]:
            try:
                normalized_source, metadata = self.normalize_source(source)
            except ValueError as exc:
                logger.warning("Skipping invalid image preview metadata: %s", exc)
                continue
            if normalized_source in seen:
                continue
            seen.add(normalized_source)
            metadata_items.append(metadata)
        return metadata_items

    def normalize_source(self, source: str) -> tuple[str, Dict[str, Any]]:
        raw_source = extract_source(str(source or "")).strip()
        if not raw_source:
            raise ValueError("Image source cannot be empty")

        source_type = classify_source(raw_source)
        if source_type == ImageSourceType.URL:
            return raw_source, self._clean_metadata({
                "external_url": raw_source,
                "mime_type": self._mime_type_from_source(raw_source),
            })

        if source_type == ImageSourceType.DATA_URI:
            image_bytes, mime_type = data_uri_to_bytes(raw_source)
            if self.workspace_dir is not None:
                metadata = save_image_bytes_to_workspace(image_bytes, mime_type, self.workspace_dir)
                return str(metadata["blob_url"]), self._clean_metadata(metadata)
            return bytes_to_data_uri(image_bytes, mime_type), self._clean_metadata({
                "mime_type": mime_type,
                "size_bytes": len(image_bytes),
            })

        if source_type == ImageSourceType.WORKSPACE_BLOB:
            relative_path = workspace_blob_relative_path(raw_source)
            if not relative_path:
                raise ValueError("Invalid workspace image blob URL")
            metadata: Dict[str, Any] = {
                "workspace_path": relative_path,
                "blob_url": workspace_blob_url(relative_path),
                "mime_type": self._mime_type_from_source(relative_path),
            }
            if self.workspace_dir is not None:
                image_path = resolve_workspace_blob_path(raw_source, self.workspace_dir)
                if image_path is None:
                    raise ValueError("Invalid workspace image blob URL")
                image_bytes, mime_type = read_image_file_bytes(image_path, allowed_mime_types=None)
                metadata.update({
                    "mime_type": mime_type,
                    "size_bytes": len(image_bytes),
                    "original_name": image_path.name,
                })
            return str(metadata["blob_url"]), self._clean_metadata(metadata)

        image_path = self.resolve_local_image_path(raw_source, workspace_dir=self.workspace_dir)
        image_bytes, mime_type = read_image_file_bytes(image_path)
        if self.workspace_dir is not None:
            resolved_path = image_path.resolve()
            if resolved_path.is_relative_to(self.workspace_dir):
                relative_path = resolved_path.relative_to(self.workspace_dir).as_posix()
                metadata = {
                    "workspace_path": relative_path,
                    "blob_url": workspace_blob_url(relative_path),
                    "mime_type": mime_type,
                    "size_bytes": len(image_bytes),
                    "original_name": resolved_path.name,
                }
                return str(metadata["blob_url"]), self._clean_metadata(metadata)
            metadata = save_image_bytes_to_workspace(
                image_bytes,
                mime_type,
                self.workspace_dir,
                original_name=image_path.name,
            )
            return str(metadata["blob_url"]), self._clean_metadata(metadata)
        return bytes_to_data_uri(image_bytes, mime_type), self._clean_metadata({
            "mime_type": mime_type,
            "size_bytes": len(image_bytes),
            "original_name": image_path.name,
        })

    @classmethod
    def attachments_from_image_metadata(cls, image_metadata: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        attachments: List[Dict[str, Any]] = []
        for metadata in image_metadata:
            workspace_path = str(metadata.get("workspace_path") or "").strip()
            blob_url = str(metadata.get("blob_url") or "").strip()
            if not workspace_path and not blob_url:
                continue
            file_name = str(metadata.get("original_name") or Path(workspace_path).name or "image").strip()
            attachments.append({
                "kind": "image",
                "path": workspace_path,
                "blob_url": blob_url,
                "mime_type": str(metadata.get("mime_type") or "image/png").strip(),
                "file_name": file_name,
                "size_bytes": metadata.get("size_bytes"),
            })
        return dedupe_attachments(attachments)

    @classmethod
    def current_message_images(
        cls,
        message: Optional[Message],
        current_user_id: str,
        *,
        workspace_dir: Optional[Union[str, Path]] = None,
    ) -> List[str]:
        if message is None or message.role != RoleType.USER or message.sender_id != current_user_id:
            return []
        if not message.images:
            return []
        return [
            image_source
            for image in message.images
            if (image_source := cls.model_image_source(image, workspace_dir=workspace_dir))
        ]

    @classmethod
    def model_image_source(cls, image: Any, *, workspace_dir: Optional[Union[str, Path]] = None) -> str:
        source = extract_source(str(getattr(image, "source", None) or image or "")).strip()
        if not source:
            return ""

        source_type = classify_source(source)
        if source_type == ImageSourceType.URL:
            return source
        if source_type == ImageSourceType.DATA_URI:
            image_bytes, mime_type = data_uri_to_bytes(source)
            return bytes_to_data_uri(image_bytes, mime_type)

        if source_type == ImageSourceType.WORKSPACE_BLOB:
            if workspace_dir is None:
                raise ValueError("Workspace image blob input requires a configured workspace directory")
            image_path = resolve_workspace_blob_path(source, workspace_dir)
            if image_path is None:
                raise ValueError("Invalid workspace image blob URL")
        else:
            image_path = cls.resolve_local_image_path(source, workspace_dir=workspace_dir)

        image_bytes, mime_type = read_image_file_bytes(image_path, allowed_mime_types=None)
        return bytes_to_data_uri(image_bytes, mime_type)

    @staticmethod
    def resolve_local_image_path(
        source: str,
        *,
        workspace_dir: Optional[Union[str, Path]] = None,
    ) -> Path:
        raw_path = Path(source).expanduser()
        if workspace_dir is not None and not raw_path.is_absolute():
            root = Path(workspace_dir).expanduser().resolve()
            workspace_path = (root / source).resolve()
            if workspace_path.is_relative_to(root) and workspace_path.exists():
                return workspace_path
        return raw_path.resolve()

    @staticmethod
    def _clean_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in metadata.items() if value not in (None, "")}

    @staticmethod
    def _mime_type_from_source(source: str) -> str:
        image_format = infer_format(source)
        if image_format == "jpeg":
            return "image/jpeg"
        if image_format == "webp":
            return "image/webp"
        if image_format == "gif":
            return "image/gif"
        return "image/png"
