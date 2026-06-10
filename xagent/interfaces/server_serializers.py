"""Serialization helpers for HTTP server payloads."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..schemas import Message
from ..schemas.attachment import ATTACHMENT_METADATA_KEY, dedupe_attachments
from ..utils.image_utils import workspace_blob_relative_path, workspace_blob_url


def response_payload(response: Any) -> Any:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return str(response)


def message_item(message: Message) -> Dict[str, Any]:
    images = message_images(message)
    attachments = message_attachments(message)
    item = {
        "role": message.role.value if hasattr(message.role, "value") else str(message.role),
        "type": message.type.value if hasattr(message.type, "value") else str(message.type),
        "content": message.content,
        "sender_id": message.sender_id,
        "timestamp": message.timestamp,
        "metadata": message.metadata,
        "images": images,
        "image_count": len(images),
        "attachments": attachments,
        "attachment_count": len(attachments),
    }
    if message.tool_call:
        item["tool_call"] = {
            "name": message.tool_call.name,
            "arguments": message.tool_call.arguments,
            "output": message.tool_call.output,
        }
    return item


def message_attachments(message: Message) -> List[Dict[str, Any]]:
    metadata_attachments = message.metadata.get(ATTACHMENT_METADATA_KEY) if isinstance(message.metadata, dict) else None
    if not isinstance(metadata_attachments, list):
        return []
    return dedupe_attachments(metadata_attachments)


def message_images(message: Message) -> List[Dict[str, Any]]:
    attachment_images: List[Dict[str, Any]] = []
    for attachment in message_attachments(message):
        if attachment.get("kind") != "image":
            continue
        item = {
            "workspace_path": attachment.get("path"),
            "blob_url": attachment.get("blob_url"),
            "mime_type": attachment.get("mime_type"),
            "size_bytes": attachment.get("size_bytes"),
            "original_name": attachment.get("file_name"),
        }
        attachment_images.append({key: value for key, value in item.items() if value not in (None, "")})

    metadata_images = message.metadata.get("images") if isinstance(message.metadata, dict) else None
    if isinstance(metadata_images, list):
        images = [
            {key: value for key, value in dict(image).items() if value not in (None, "")}
            for image in metadata_images
            if isinstance(image, dict)
        ]
        return _dedupe_image_items([*images, *attachment_images])

    if not message.multimodal or not message.multimodal.image:
        return attachment_images

    images = message.multimodal.image if isinstance(message.multimodal.image, list) else [message.multimodal.image]
    result: List[Dict[str, Any]] = []
    for image in images:
        source = str(getattr(image, "source", "") or "")
        if not source:
            continue
        item: Dict[str, Any] = {"mime_type": _image_mime_type(source, getattr(image, "format", ""))}
        relative_path = workspace_blob_relative_path(source)
        if relative_path:
            item["workspace_path"] = relative_path
            item["blob_url"] = workspace_blob_url(relative_path)
        elif source.startswith(("http://", "https://")):
            item["external_url"] = source
        result.append({key: value for key, value in item.items() if value not in (None, "")})
    return _dedupe_image_items([*result, *attachment_images])


def message_search_result(message: Message, query: str) -> Optional[Dict[str, Any]]:
    normalized_query = query.strip().lower()
    if not normalized_query:
        return None

    matched_in: List[str] = []
    snippet = ""
    for field, text in _message_search_fields(message):
        if not text:
            continue
        if normalized_query not in text.lower():
            continue
        matched_in.append(field)
        if not snippet:
            snippet = _build_search_snippet(text, query)

    if not matched_in:
        return None

    return {
        **message_item(message),
        "matched_in": matched_in,
        "snippet": snippet,
    }


def _dedupe_image_items(images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for image in images:
        key = str(image.get("blob_url") or image.get("workspace_path") or image.get("external_url") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(image)
    return deduped


def _image_mime_type(source: str, image_format: str = "") -> str:
    if source.startswith("data:image/"):
        return source.split(";", 1)[0].removeprefix("data:").lower()
    normalized_format = str(image_format or "").strip().lower()
    if normalized_format == "jpeg":
        return "image/jpeg"
    if normalized_format == "webp":
        return "image/webp"
    if normalized_format == "gif":
        return "image/gif"
    return "image/png"


def _message_search_fields(message: Message) -> List[tuple[str, str]]:
    role = message.role.value if hasattr(message.role, "value") else str(message.role)
    message_type = message.type.value if hasattr(message.type, "value") else str(message.type)
    fields: List[tuple[str, str]] = [
        ("content", message.content or ""),
        ("sender", message.sender_id or ""),
        ("role", role),
        ("type", message_type),
    ]

    if message.tool_call:
        tool_parts = [
            str(message.tool_call.name or ""),
            str(message.tool_call.arguments or ""),
            str(message.tool_call.output or ""),
        ]
        tool_text = " ".join(part for part in tool_parts if part)
        if tool_text:
            fields.append(("tool", tool_text))

    if message.metadata:
        metadata_text = json.dumps(message.metadata, ensure_ascii=False, sort_keys=True, default=str)
        fields.append(("metadata", metadata_text))

    return fields


def _build_search_snippet(text: str, query: str) -> str:
    if not text:
        return ""

    normalized_query = query.strip().lower()
    lower_text = text.lower()
    match_index = lower_text.find(normalized_query)
    if match_index == -1:
        return text[:200].replace("\n", " ").strip()

    start = max(0, match_index - 80)
    end = min(len(text), match_index + len(query) + 120)
    return text[start:end].replace("\n", " ").strip()