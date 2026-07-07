"""Normalize chat attachments and image sources for the api channel."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from fastapi import HTTPException

from ...interfaces.server.models import ChatInput
from ...schemas.attachment import (
    MAX_MESSAGE_ATTACHMENT_BYTES,
    attachment_image_sources,
    dedupe_attachments,
)
from ...utils.image_utils import MAX_IMAGES_PER_MESSAGE, data_uri_to_bytes
from .constants import CLIENT_WEB


def input_image_sources(
    input_data: ChatInput,
    *,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Union[str, List[str]]]:
    sources: List[str] = []
    raw_source = input_data.image_source
    if raw_source:
        if isinstance(raw_source, list):
            sources.extend(str(item) for item in raw_source if str(item or "").strip())
        else:
            sources.append(str(raw_source))
    for image in input_data.images or []:
        source = image.blob_url or image.external_url or image.workspace_path or ""
        if source:
            sources.append(source)
    sources.extend(attachment_image_sources(attachments or []))
    deduped_sources: List[str] = []
    seen_sources: set[str] = set()
    for source in sources:
        normalized = str(source or "").strip()
        if normalized and normalized not in seen_sources:
            seen_sources.add(normalized)
            deduped_sources.append(normalized)
    sources = deduped_sources
    if not sources:
        return None
    if len(sources) > MAX_IMAGES_PER_MESSAGE:
        raise HTTPException(
            status_code=413,
            detail=f"At most {MAX_IMAGES_PER_MESSAGE} images are allowed per message",
        )
    for source in sources:
        if str(source).startswith("data:image/"):
            try:
                data_uri_to_bytes(source)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    return sources[0] if len(sources) == 1 else sources


def input_attachments(input_data: ChatInput) -> Optional[List[Dict[str, Any]]]:
    raw_attachments: List[Dict[str, Any]] = []
    for attachment in input_data.attachments or []:
        raw_attachments.append(attachment.model_dump(exclude_none=True))
    for image in input_data.images or []:
        raw_attachments.append({
            "kind": "image",
            "path": image.workspace_path,
            "blob_url": image.blob_url,
            "mime_type": image.mime_type,
            "file_name": image.original_name,
            "size_bytes": image.size_bytes,
            "client": CLIENT_WEB,
        })
    attachments = dedupe_attachments(raw_attachments)
    if not attachments:
        return None
    total_size = sum(int(attachment.get("size_bytes") or 0) for attachment in attachments)
    if total_size > MAX_MESSAGE_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="Message attachments exceed 200MB")
    return attachments
