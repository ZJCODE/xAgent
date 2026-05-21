"""Configurable image generation tool implementations."""

from __future__ import annotations

import base64
import binascii
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from openai import AsyncOpenAI

from xagent.utils.tool_decorator import function_tool


logger = logging.getLogger(__name__)

IMAGE_GENERATION_PROVIDER_OPENAI = "openai"
IMAGE_GENERATION_PROVIDER_NONE = "none"
SUPPORTED_IMAGE_GENERATION_PROVIDERS = {
    IMAGE_GENERATION_PROVIDER_OPENAI,
    IMAGE_GENERATION_PROVIDER_NONE,
}

DEFAULT_IMAGE_GENERATION_MODEL = "gpt-image-1"
DEFAULT_IMAGE_GENERATION_SIZE = "1024x1024"
DEFAULT_IMAGE_GENERATION_QUALITY = "auto"
DEFAULT_IMAGE_GENERATION_FORMAT = "png"
DEFAULT_IMAGE_GENERATION_BACKGROUND = "auto"
IMAGE_GENERATION_OUTPUT_DIR = "temp/images"


@dataclass(frozen=True)
class ConfiguredImageGenerationProvider:
    """Dispatch image generation calls to the configured provider."""

    provider: str
    config: dict
    client: Optional[AsyncOpenAI]
    workspace_dir: Path

    async def generate(
        self,
        prompt: str,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        output_format: Optional[str] = None,
        background: Optional[str] = None,
        output_compression: Optional[int] = None,
    ) -> dict:
        prompt = prompt.strip()
        if not prompt:
            return _error_response(self.provider, "prompt is required")
        if self.provider == IMAGE_GENERATION_PROVIDER_OPENAI:
            return await self._generate_openai(
                prompt=prompt,
                size=size,
                quality=quality,
                output_format=output_format,
                background=background,
                output_compression=output_compression,
            )
        return _error_response(self.provider, "image generation is disabled")

    async def _generate_openai(
        self,
        *,
        prompt: str,
        size: Optional[str],
        quality: Optional[str],
        output_format: Optional[str],
        background: Optional[str],
        output_compression: Optional[int],
    ) -> dict:
        image_client = self.client
        if image_client is None:
            try:
                image_client = AsyncOpenAI()
            except Exception as exception:
                return _error_response(self.provider, f"OpenAI client is not configured: {exception}")

        image_format = _normalize_output_format(
            output_format or self.config.get("output_format") or DEFAULT_IMAGE_GENERATION_FORMAT
        )
        params: dict[str, Any] = {
            "model": self.config.get("model") or DEFAULT_IMAGE_GENERATION_MODEL,
            "prompt": prompt,
            "size": _clean_optional(size or self.config.get("size") or DEFAULT_IMAGE_GENERATION_SIZE),
            "quality": _clean_optional(quality or self.config.get("quality") or DEFAULT_IMAGE_GENERATION_QUALITY),
            "output_format": image_format,
        }
        normalized_background = _clean_optional(background or self.config.get("background") or DEFAULT_IMAGE_GENERATION_BACKGROUND)
        if normalized_background:
            params["background"] = normalized_background

        normalized_compression = _normalize_output_compression(
            output_compression if output_compression is not None else self.config.get("output_compression")
        )
        if normalized_compression is not None:
            params["output_compression"] = normalized_compression

        try:
            response = await image_client.images.generate(**params)
        except Exception as exception:
            logger.warning("OpenAI image generation failed: %s", exception)
            return _error_response(self.provider, str(exception))

        image_base64, revised_prompt = _extract_image_response(response)
        if not image_base64:
            return _error_response(self.provider, "OpenAI response did not contain base64 image data")

        try:
            image_bytes = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as exception:
            return _error_response(self.provider, f"OpenAI returned invalid image data: {exception}")

        output_path = self._write_image_file(image_bytes, image_format)
        relative_path = output_path.relative_to(self.workspace_dir).as_posix()
        blob_url = f"/api/workspace/blob?path={quote(relative_path)}"
        markdown = f"![Generated image]({blob_url})"

        return {
            "status": "ok",
            "type": "generated_image",
            "provider": self.provider,
            "prompt": prompt,
            "revised_prompt": revised_prompt,
            "model": params["model"],
            "size": params.get("size"),
            "quality": params.get("quality"),
            "background": params.get("background"),
            "output_format": image_format,
            "image": {
                "path": relative_path,
                "blob_url": blob_url,
                "markdown": markdown,
                "format": image_format,
                "mime_type": _mime_type(image_format),
                "bytes": len(image_bytes),
            },
        }

    def _write_image_file(self, image_bytes: bytes, image_format: str) -> Path:
        output_dir = self.workspace_dir / IMAGE_GENERATION_OUTPUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.{_extension(image_format)}"
        output_path = output_dir / filename
        output_path.write_bytes(image_bytes)
        return output_path


def create_image_generation_tool(
    image_generation_config: Optional[dict],
    *,
    client: Optional[AsyncOpenAI] = None,
    workspace_dir: str,
):
    """Create the configured generate_image tool, or return None when disabled."""
    config = image_generation_config or {}
    provider = normalize_image_generation_provider(config.get("provider"))
    if provider == IMAGE_GENERATION_PROVIDER_NONE:
        return None

    image_provider = ConfiguredImageGenerationProvider(
        provider=provider,
        config=config,
        client=client,
        workspace_dir=Path(workspace_dir).expanduser().resolve(),
    )

    @function_tool(
        name="generate_image",
        description=(
            "Generate a new image from a text prompt using the configured image generation provider. "
            "Use it when the user asks to create, draw, render, or generate a visual asset."
        ),
        param_descriptions={
            "prompt": "Detailed image prompt describing subject, style, composition, and constraints.",
            "size": "Optional output size such as 1024x1024, 1024x1536, 1536x1024, or auto.",
            "quality": "Optional quality value such as low, medium, high, or auto.",
            "output_format": "Optional output format: png, jpeg, or webp.",
            "background": "Optional background value such as auto, opaque, or transparent when supported.",
            "output_compression": "Optional JPEG/WebP compression level from 0 to 100.",
        },
    )
    async def generate_image(
        prompt: str,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        output_format: Optional[str] = None,
        background: Optional[str] = None,
        output_compression: Optional[int] = None,
    ) -> dict:
        return await image_provider.generate(
            prompt=prompt,
            size=size,
            quality=quality,
            output_format=output_format,
            background=background,
            output_compression=output_compression,
        )

    return generate_image


def normalize_image_generation_provider(provider: Any) -> str:
    normalized = str(provider or IMAGE_GENERATION_PROVIDER_NONE).strip().lower().replace("-", "_")
    aliases = {
        "off": IMAGE_GENERATION_PROVIDER_NONE,
        "disabled": IMAGE_GENERATION_PROVIDER_NONE,
        "no_image_generation": IMAGE_GENERATION_PROVIDER_NONE,
        "none": IMAGE_GENERATION_PROVIDER_NONE,
        "openai_image_generation": IMAGE_GENERATION_PROVIDER_OPENAI,
        "openai_images": IMAGE_GENERATION_PROVIDER_OPENAI,
        "openai": IMAGE_GENERATION_PROVIDER_OPENAI,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_IMAGE_GENERATION_PROVIDERS:
        raise ValueError(f"Unsupported image generation provider: {provider}")
    return normalized


def is_generated_image_result(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("status") == "ok"
        and result.get("type") == "generated_image"
        and isinstance(result.get("image"), dict)
        and bool(result["image"].get("markdown"))
    )


def generated_image_markdown(result: dict) -> str:
    return str(result.get("image", {}).get("markdown") or "")


def generated_image_description(tool_name: str, result: dict) -> str:
    prompt = str(result.get("prompt") or "").strip()
    revised_prompt = str(result.get("revised_prompt") or "").strip()
    path = str(result.get("image", {}).get("path") or "").strip()
    description = f"[Image generated by tool `{tool_name}` and displayed to user."
    if path:
        description += f" Saved path: {path}."
    if prompt:
        description += f" Prompt: {prompt}."
    if revised_prompt and revised_prompt != prompt:
        description += f" Revised prompt: {revised_prompt}."
    return description + "]"


def _extract_image_response(response: Any) -> tuple[str, str]:
    for item in _field(response, "data", []) or []:
        image_base64 = _field(item, "b64_json") or _field(item, "result") or ""
        revised_prompt = _field(item, "revised_prompt") or ""
        if image_base64:
            return str(image_base64), str(revised_prompt or "")
    return "", ""


def _error_response(provider: str, message: str) -> dict:
    return {
        "status": "error",
        "provider": provider,
        "message": message,
    }


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _clean_optional(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _normalize_output_format(value: Any) -> str:
    normalized = str(value or DEFAULT_IMAGE_GENERATION_FORMAT).strip().lower()
    if normalized in {"jpg", "jpeg"}:
        return "jpeg"
    if normalized in {"png", "webp"}:
        return normalized
    return DEFAULT_IMAGE_GENERATION_FORMAT


def _normalize_output_compression(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        compression = int(value)
    except (TypeError, ValueError):
        return None
    return min(100, max(0, compression))


def _mime_type(image_format: str) -> str:
    if image_format == "jpeg":
        return "image/jpeg"
    if image_format == "webp":
        return "image/webp"
    return "image/png"


def _extension(image_format: str) -> str:
    if image_format == "jpeg":
        return "jpg"
    if image_format == "webp":
        return "webp"
    return "png"