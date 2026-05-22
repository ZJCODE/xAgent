"""Configurable image generation tool implementations."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from openai import AsyncOpenAI

from xagent.utils.image_utils import workspace_blob_url
from xagent.utils.tool_decorator import function_tool


logger = logging.getLogger(__name__)

IMAGE_GENERATION_PROVIDER_OPENAI = "openai"
IMAGE_GENERATION_PROVIDER_MINIMAX = "minimax"
IMAGE_GENERATION_PROVIDER_QWEN = "qwen"
IMAGE_GENERATION_PROVIDER_NONE = "none"
SUPPORTED_IMAGE_GENERATION_PROVIDERS = {
    IMAGE_GENERATION_PROVIDER_OPENAI,
    IMAGE_GENERATION_PROVIDER_MINIMAX,
    IMAGE_GENERATION_PROVIDER_QWEN,
    IMAGE_GENERATION_PROVIDER_NONE,
}

DEFAULT_IMAGE_GENERATION_MODEL = "gpt-image-2"
DEFAULT_IMAGE_GENERATION_SIZE = "auto"
DEFAULT_IMAGE_GENERATION_QUALITY = "auto"
DEFAULT_IMAGE_GENERATION_FORMAT = "png"
DEFAULT_IMAGE_GENERATION_BACKGROUND = "auto"
DEFAULT_MINIMAX_IMAGE_GENERATION_MODEL = "image-01"
DEFAULT_MINIMAX_IMAGE_GENERATION_ASPECT_RATIO = "1:1"
DEFAULT_QWEN_IMAGE_GENERATION_MODEL = "qwen-image-2.0-pro"
DEFAULT_QWEN_IMAGE_GENERATION_SIZE = "2048*2048"
IMAGE_GENERATION_OUTPUT_DIR = "temp/images"
MINIMAX_IMAGE_GENERATION_ENDPOINT = "https://api.minimaxi.com/v1/image_generation"
MINIMAX_IMAGE_GENERATION_TIMEOUT = 180.0
QWEN_IMAGE_GENERATION_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
QWEN_IMAGE_GENERATION_TIMEOUT = 300.0
MINIMAX_API_KEY_ENV_VARS = ("MINIMAX_API_KEY", "MINIMAX_API_TOKEN")
QWEN_API_KEY_ENV_VARS = ("DASHSCOPE_API_KEY", "DASHSCOPE_API_TOKEN", "QWEN_API_KEY", "QWEN_API_TOKEN")
MINIMAX_IMAGE_GENERATION_ASPECT_RATIOS = {
    "1:1",
    "16:9",
    "4:3",
    "3:2",
    "2:3",
    "3:4",
    "9:16",
    "21:9",
}
PLACEHOLDER_API_KEYS = {
    "your_api_key",
    "your_api_key_here",
    "your_openai_api_key",
    "your_openai_api_key_here",
    "your_minimax_api_key",
    "your_minimax_api_key_here",
    "your_qwen_api_key",
    "your_qwen_api_key_here",
    "your_dashscope_api_key",
    "your_dashscope_api_key_here",
}


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
        aspect_ratio: Optional[str] = None,
        reference_image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None,
        n: Optional[int] = None,
        seed: Optional[int] = None,
        negative_prompt: Optional[str] = None,
        prompt_extend: Optional[bool] = None,
        watermark: Optional[bool] = None,
        prompt_optimizer: Optional[bool] = None,
        aigc_watermark: Optional[bool] = None,
        moderation: Optional[str] = None,
    ) -> dict:
        prompt = prompt.strip()
        if not prompt:
            return _error_response(self.provider, "prompt is required")
        if self.provider == IMAGE_GENERATION_PROVIDER_OPENAI:
            unsupported = {
                "aspect_ratio": aspect_ratio,
                "reference_image_url": reference_image_url,
                "reference_image_urls": reference_image_urls,
                "seed": seed,
                "negative_prompt": negative_prompt,
                "prompt_extend": prompt_extend,
                "watermark": watermark,
                "prompt_optimizer": prompt_optimizer,
                "aigc_watermark": aigc_watermark,
            }
            unsupported_names = [name for name, value in unsupported.items() if value not in (None, "", [])]
            if unsupported_names:
                return _error_response(
                    self.provider,
                    f"OpenAI image generation does not support parameter(s): {', '.join(unsupported_names)}",
                )
            return await self._generate_openai(
                prompt=prompt,
                size=size,
                quality=quality,
                output_format=output_format,
                background=background,
                output_compression=output_compression,
                n=n,
                moderation=moderation,
            )
        if self.provider == IMAGE_GENERATION_PROVIDER_MINIMAX:
            unsupported = {
                "quality": quality,
                "output_format": output_format,
                "background": background,
                "output_compression": output_compression,
                "moderation": moderation,
                "negative_prompt": negative_prompt,
                "prompt_extend": prompt_extend,
                "watermark": watermark,
            }
            unsupported_names = [name for name, value in unsupported.items() if value not in (None, "", [])]
            if unsupported_names:
                return _error_response(
                    self.provider,
                    f"MiniMax image generation does not support parameter(s): {', '.join(unsupported_names)}",
                )
            return await self._generate_minimax(
                prompt=prompt,
                size=size,
                aspect_ratio=aspect_ratio,
                reference_image_url=reference_image_url,
                reference_image_urls=reference_image_urls,
                n=n,
                seed=seed,
                prompt_optimizer=prompt_optimizer,
                aigc_watermark=aigc_watermark,
            )
        if self.provider == IMAGE_GENERATION_PROVIDER_QWEN:
            unsupported = {
                "quality": quality,
                "output_format": output_format,
                "background": background,
                "output_compression": output_compression,
                "aspect_ratio": aspect_ratio,
                "reference_image_url": reference_image_url,
                "reference_image_urls": reference_image_urls,
                "moderation": moderation,
            }
            unsupported_names = [name for name, value in unsupported.items() if value not in (None, "", [])]
            if unsupported_names:
                return _error_response(
                    self.provider,
                    f"Qwen image generation does not support parameter(s): {', '.join(unsupported_names)}",
                )
            return await self._generate_qwen(
                prompt=prompt,
                size=size,
                n=n,
                seed=seed,
                negative_prompt=negative_prompt,
                prompt_extend=prompt_extend if prompt_extend is not None else prompt_optimizer,
                watermark=watermark if watermark is not None else aigc_watermark,
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
        n: Optional[int],
        moderation: Optional[str],
    ) -> dict:
        image_client = self.client
        if image_client is None:
            try:
                image_client = AsyncOpenAI()
            except Exception as exception:
                return _error_response(self.provider, f"OpenAI client is not configured: {exception}")

        model = str(self.config.get("model") or DEFAULT_IMAGE_GENERATION_MODEL).strip()
        try:
            image_format = _normalize_output_format(
                output_format or self.config.get("output_format") or DEFAULT_IMAGE_GENERATION_FORMAT
            )
            params: dict[str, Any] = {
                "model": model,
                "prompt": prompt,
                "size": _normalize_openai_size(size or self.config.get("size") or DEFAULT_IMAGE_GENERATION_SIZE),
                "quality": _normalize_openai_quality(quality or self.config.get("quality") or DEFAULT_IMAGE_GENERATION_QUALITY),
                "output_format": image_format,
            }
            normalized_background = _normalize_openai_background(
                background or self.config.get("background") or DEFAULT_IMAGE_GENERATION_BACKGROUND
            )
        except ValueError as exception:
            return _error_response(self.provider, str(exception))
        if normalized_background:
            if _is_gpt_image_2(model) and normalized_background == "transparent":
                return _error_response(
                    self.provider,
                    "gpt-image-2 does not support transparent backgrounds; use auto, opaque, or another GPT Image model",
                )
            params["background"] = normalized_background

        try:
            normalized_compression = _normalize_output_compression(
                output_compression if output_compression is not None else self.config.get("output_compression")
            )
        except ValueError as exception:
            return _error_response(self.provider, str(exception))
        if normalized_compression is not None and image_format in {"jpeg", "webp"}:
            params["output_compression"] = normalized_compression
        try:
            normalized_count = _normalize_count(n if n is not None else self.config.get("n"), max_count=10)
        except ValueError as exception:
            return _error_response(self.provider, str(exception))
        if normalized_count is not None:
            params["n"] = normalized_count
        try:
            normalized_moderation = _normalize_openai_moderation(moderation or self.config.get("moderation"))
        except ValueError as exception:
            return _error_response(self.provider, str(exception))
        if normalized_moderation:
            params["moderation"] = normalized_moderation

        try:
            response = await image_client.images.generate(**params)
        except Exception as exception:
            logger.warning("OpenAI image generation failed: %s", exception)
            return _error_response(self.provider, str(exception))

        image_responses = _extract_image_responses(response)
        if not image_responses:
            return _error_response(self.provider, "OpenAI response did not contain base64 image data")

        saved_images: list[dict] = []
        revised_prompt = ""
        for image_base64, item_revised_prompt in image_responses:
            try:
                image_bytes = base64.b64decode(image_base64, validate=True)
            except (binascii.Error, ValueError) as exception:
                return _error_response(self.provider, f"OpenAI returned invalid image data: {exception}")
            if item_revised_prompt and not revised_prompt:
                revised_prompt = item_revised_prompt
            saved_images.append(self._save_image_file(image_bytes, image_format))

        return _generated_image_response(
            provider=self.provider,
            prompt=prompt,
            revised_prompt=revised_prompt,
            model=params["model"],
            size=params.get("size"),
            quality=params.get("quality"),
            background=params.get("background"),
            output_format=image_format,
            images=saved_images,
            extra={
                "moderation": params.get("moderation"),
                "n": params.get("n"),
            },
        )

    async def _generate_minimax(
        self,
        *,
        prompt: str,
        size: Optional[str],
        aspect_ratio: Optional[str],
        reference_image_url: Optional[str],
        reference_image_urls: Optional[list[str]],
        n: Optional[int],
        seed: Optional[int],
        prompt_optimizer: Optional[bool],
        aigc_watermark: Optional[bool],
    ) -> dict:
        api_key = _get_minimax_api_key(self.config)
        if not api_key:
            return _error_response(
                self.provider,
                "MiniMax image generation requires image_generation.api_key or MINIMAX_API_KEY",
            )

        payload: dict[str, Any] = {
            "model": self.config.get("model") or DEFAULT_MINIMAX_IMAGE_GENERATION_MODEL,
            "prompt": prompt,
            "response_format": "base64",
        }
        try:
            requested_aspect_ratio = _clean_optional(aspect_ratio or self.config.get("aspect_ratio"))
            normalized_width = _normalize_minimax_dimension(self.config.get("width"))
            normalized_height = _normalize_minimax_dimension(self.config.get("height"))
            if requested_aspect_ratio:
                payload["aspect_ratio"] = _normalize_minimax_aspect_ratio(requested_aspect_ratio)
            elif normalized_width is not None and normalized_height is not None:
                payload["width"] = normalized_width
                payload["height"] = normalized_height
            else:
                payload["aspect_ratio"] = (
                    _minimax_aspect_ratio_from_size(size or self.config.get("size"))
                    or DEFAULT_MINIMAX_IMAGE_GENERATION_ASPECT_RATIO
                )

            normalized_count = _normalize_count(n if n is not None else self.config.get("n"), max_count=9)
        except ValueError as exception:
            return _error_response(self.provider, str(exception))
        if normalized_count is not None:
            payload["n"] = normalized_count
        normalized_seed = _normalize_int(seed if seed is not None else self.config.get("seed"))
        if normalized_seed is not None:
            payload["seed"] = normalized_seed
        try:
            normalized_prompt_optimizer = _normalize_optional_bool(
                prompt_optimizer if prompt_optimizer is not None else self.config.get("prompt_optimizer")
            )
        except ValueError as exception:
            return _error_response(self.provider, str(exception))
        if normalized_prompt_optimizer is not None:
            payload["prompt_optimizer"] = normalized_prompt_optimizer
        try:
            normalized_watermark = _normalize_optional_bool(
                aigc_watermark if aigc_watermark is not None else self.config.get("aigc_watermark")
            )
        except ValueError as exception:
            return _error_response(self.provider, str(exception))
        if normalized_watermark is not None:
            payload["aigc_watermark"] = normalized_watermark

        subject_reference = self.config.get("subject_reference")
        if isinstance(subject_reference, list) and subject_reference:
            payload["subject_reference"] = subject_reference
        else:
            reference_urls = _normalize_reference_image_urls(
                reference_image_url,
                reference_image_urls,
                self.config.get("reference_image_url"),
                self.config.get("reference_image_urls"),
            )
            if reference_urls:
                payload["subject_reference"] = [
                    {"type": "character", "image_file": image_url}
                    for image_url in reference_urls
                ]

        style = self.config.get("style")
        if isinstance(style, dict) and style:
            payload["style"] = style

        endpoint = _minimax_image_generation_endpoint(self.config)
        try:
            async with httpx.AsyncClient(timeout=MINIMAX_IMAGE_GENERATION_TIMEOUT) as http_client:
                response = await http_client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                response_json = response.json()
        except httpx.HTTPStatusError as exception:
            response_text = exception.response.text[:300] if exception.response is not None else str(exception)
            logger.warning("MiniMax image generation failed: %s", response_text)
            return _error_response(self.provider, response_text)
        except Exception as exception:
            logger.warning("MiniMax image generation failed: %s", exception)
            return _error_response(self.provider, str(exception))

        base_resp = _field(response_json, "base_resp", {}) or {}
        status_code = _field(base_resp, "status_code")
        if status_code not in (None, 0, "0"):
            status_message = _field(base_resp, "status_msg", "MiniMax image generation failed")
            return _error_response(self.provider, str(status_message))

        image_base64_values = _extract_minimax_base64_images(response_json)
        if not image_base64_values:
            return _error_response(self.provider, "MiniMax response did not contain base64 image data")

        saved_images: list[dict] = []
        for image_base64 in image_base64_values:
            try:
                image_bytes = base64.b64decode(image_base64, validate=True)
            except (binascii.Error, ValueError) as exception:
                return _error_response(self.provider, f"MiniMax returned invalid image data: {exception}")
            image_format = _detect_image_format(image_bytes, fallback="jpeg")
            saved_images.append(self._save_image_file(image_bytes, image_format))

        return _generated_image_response(
            provider=self.provider,
            prompt=prompt,
            revised_prompt="",
            model=payload["model"],
            size=size or self.config.get("size"),
            quality=None,
            background=None,
            output_format=saved_images[0]["format"],
            images=saved_images,
            extra={
                "aspect_ratio": payload.get("aspect_ratio"),
                "width": payload.get("width"),
                "height": payload.get("height"),
                "n": payload.get("n"),
                "seed": payload.get("seed"),
                "prompt_optimizer": payload.get("prompt_optimizer"),
                "aigc_watermark": payload.get("aigc_watermark"),
            },
        )

    async def _generate_qwen(
        self,
        *,
        prompt: str,
        size: Optional[str],
        n: Optional[int],
        seed: Optional[int],
        negative_prompt: Optional[str],
        prompt_extend: Optional[bool],
        watermark: Optional[bool],
    ) -> dict:
        api_key = _get_qwen_api_key(self.config)
        if not api_key:
            return _error_response(
                self.provider,
                "Qwen image generation requires image_generation.api_key, provider.api_key, or DASHSCOPE_API_KEY",
            )

        model = str(self.config.get("model") or DEFAULT_QWEN_IMAGE_GENERATION_MODEL).strip()
        try:
            normalized_size = _normalize_qwen_size(size or self.config.get("size") or DEFAULT_QWEN_IMAGE_GENERATION_SIZE)
            normalized_count = _normalize_qwen_count(n if n is not None else self.config.get("n"), model=model)
            normalized_seed = _normalize_qwen_seed(seed if seed is not None else self.config.get("seed"))
            normalized_prompt_extend = _normalize_optional_bool(
                prompt_extend if prompt_extend is not None else self.config.get("prompt_extend")
            )
            normalized_watermark = _normalize_optional_bool(
                watermark if watermark is not None else self.config.get("watermark")
            )
        except ValueError as exception:
            return _error_response(self.provider, str(exception))

        parameters: dict[str, Any] = {"size": normalized_size}
        if normalized_count is not None:
            parameters["n"] = normalized_count
        if normalized_seed is not None:
            parameters["seed"] = normalized_seed
        if normalized_prompt_extend is not None:
            parameters["prompt_extend"] = normalized_prompt_extend
        if normalized_watermark is not None:
            parameters["watermark"] = normalized_watermark
        configured_negative_prompt = _clean_optional(
            negative_prompt if negative_prompt is not None else self.config.get("negative_prompt")
        )
        if configured_negative_prompt:
            parameters["negative_prompt"] = configured_negative_prompt

        payload: dict[str, Any] = {
            "model": model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": prompt}],
                    }
                ]
            },
            "parameters": parameters,
        }
        endpoint = _qwen_image_generation_endpoint(self.config)

        try:
            async with httpx.AsyncClient(timeout=QWEN_IMAGE_GENERATION_TIMEOUT) as http_client:
                response = await http_client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                response_json = response.json()

                code = str(_field(response_json, "code") or "").strip()
                if code:
                    message = str(_field(response_json, "message", "Qwen image generation failed") or "")
                    return _error_response(self.provider, f"{code}: {message}" if message else code)

                image_urls = _extract_qwen_image_urls(response_json)
                if not image_urls:
                    return _error_response(self.provider, "Qwen response did not contain image URLs")

                saved_images: list[dict] = []
                for image_url in image_urls:
                    image_response = await http_client.get(image_url)
                    image_response.raise_for_status()
                    image_bytes = bytes(image_response.content)
                    image_format = _detect_image_format(image_bytes, fallback="png")
                    saved_images.append(self._save_image_file(image_bytes, image_format))
        except httpx.HTTPStatusError as exception:
            response_text = exception.response.text[:300] if exception.response is not None else str(exception)
            logger.warning("Qwen image generation failed: %s", response_text)
            return _error_response(self.provider, response_text)
        except Exception as exception:
            logger.warning("Qwen image generation failed: %s", exception)
            return _error_response(self.provider, str(exception))

        return _generated_image_response(
            provider=self.provider,
            prompt=prompt,
            revised_prompt=_extract_qwen_revised_prompt(response_json),
            model=model,
            size=normalized_size,
            quality=None,
            background=None,
            output_format=saved_images[0]["format"],
            images=saved_images,
            extra={
                "n": parameters.get("n"),
                "seed": parameters.get("seed"),
                "negative_prompt": parameters.get("negative_prompt"),
                "prompt_extend": parameters.get("prompt_extend"),
                "watermark": parameters.get("watermark"),
                "request_id": _field(response_json, "request_id"),
            },
        )

    def _save_image_file(self, image_bytes: bytes, image_format: str) -> dict:
        output_path = self._write_image_file(image_bytes, image_format)
        relative_path = output_path.relative_to(self.workspace_dir).as_posix()
        blob_url = workspace_blob_url(relative_path)
        return {
            "path": relative_path,
            "blob_url": blob_url,
            "markdown": f"![Generated image]({blob_url})",
            "format": image_format,
            "mime_type": _mime_type(image_format),
            "bytes": len(image_bytes),
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
            "aspect_ratio": "Optional MiniMax aspect ratio such as 1:1, 16:9, 4:3, 3:2, 2:3, 3:4, 9:16, or 21:9.",
            "reference_image_url": "Optional MiniMax reference image URL for image-to-image subject consistency.",
            "reference_image_urls": "Optional MiniMax reference image URLs for image-to-image subject consistency.",
            "n": "Optional number of images to generate.",
            "seed": "Optional MiniMax or Qwen seed for more reproducible outputs.",
            "negative_prompt": "Optional Qwen negative prompt describing content to avoid.",
            "prompt_extend": "Optional Qwen prompt rewriting toggle.",
            "watermark": "Optional Qwen watermark toggle.",
            "prompt_optimizer": "Optional MiniMax prompt optimizer toggle.",
            "aigc_watermark": "Optional MiniMax AIGC watermark toggle.",
            "moderation": "Optional OpenAI moderation strictness such as auto or low.",
        },
    )
    async def generate_image(
        prompt: str,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        output_format: Optional[str] = None,
        background: Optional[str] = None,
        output_compression: Optional[int] = None,
        aspect_ratio: Optional[str] = None,
        reference_image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None,
        n: Optional[int] = None,
        seed: Optional[int] = None,
        negative_prompt: Optional[str] = None,
        prompt_extend: Optional[bool] = None,
        watermark: Optional[bool] = None,
        prompt_optimizer: Optional[bool] = None,
        aigc_watermark: Optional[bool] = None,
        moderation: Optional[str] = None,
    ) -> dict:
        return await image_provider.generate(
            prompt=prompt,
            size=size,
            quality=quality,
            output_format=output_format,
            background=background,
            output_compression=output_compression,
            aspect_ratio=aspect_ratio,
            reference_image_url=reference_image_url,
            reference_image_urls=reference_image_urls,
            n=n,
            seed=seed,
            negative_prompt=negative_prompt,
            prompt_extend=prompt_extend,
            watermark=watermark,
            prompt_optimizer=prompt_optimizer,
            aigc_watermark=aigc_watermark,
            moderation=moderation,
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
        "minimax_image_generation": IMAGE_GENERATION_PROVIDER_MINIMAX,
        "minimax_images": IMAGE_GENERATION_PROVIDER_MINIMAX,
        "mini_max": IMAGE_GENERATION_PROVIDER_MINIMAX,
        "minimax": IMAGE_GENERATION_PROVIDER_MINIMAX,
        "qwen_image_generation": IMAGE_GENERATION_PROVIDER_QWEN,
        "qwen_images": IMAGE_GENERATION_PROVIDER_QWEN,
        "dashscope": IMAGE_GENERATION_PROVIDER_QWEN,
        "qwen": IMAGE_GENERATION_PROVIDER_QWEN,
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
    images = result.get("images")
    if isinstance(images, list):
        markdowns = [str(image.get("markdown")) for image in images if isinstance(image, dict) and image.get("markdown")]
        if markdowns:
            return "\n\n".join(markdowns)
    return str(result.get("image", {}).get("markdown") or "")


def generated_image_description(tool_name: str, result: dict) -> str:
    prompt = str(result.get("prompt") or "").strip()
    revised_prompt = str(result.get("revised_prompt") or "").strip()
    paths = _generated_image_paths(result)
    description = f"[Image generated by tool `{tool_name}` and displayed to user."
    if len(paths) == 1:
        description += f" Saved path: {paths[0]}."
    elif paths:
        description += f" Saved paths: {', '.join(paths)}."
    if prompt:
        description += f" Prompt: {prompt}."
    if revised_prompt and revised_prompt != prompt:
        description += f" Revised prompt: {revised_prompt}."
    return description + "]"


def _generated_image_paths(result: dict) -> list[str]:
    images = result.get("images")
    if isinstance(images, list):
        paths = [str(image.get("path") or "").strip() for image in images if isinstance(image, dict)]
        return [path for path in paths if path]
    path = str(result.get("image", {}).get("path") or "").strip()
    return [path] if path else []


def _generated_image_response(
    *,
    provider: str,
    prompt: str,
    revised_prompt: str,
    model: str,
    size: Any,
    quality: Any,
    background: Any,
    output_format: str,
    images: list[dict],
    extra: Optional[dict[str, Any]] = None,
) -> dict:
    response = {
        "status": "ok",
        "type": "generated_image",
        "provider": provider,
        "prompt": prompt,
        "revised_prompt": revised_prompt,
        "model": model,
        "size": size,
        "quality": quality,
        "background": background,
        "output_format": output_format,
        "image": images[0],
        "images": images,
    }
    if extra:
        response.update({key: value for key, value in extra.items() if value is not None})
    return response


def _extract_image_responses(response: Any) -> list[tuple[str, str]]:
    image_responses: list[tuple[str, str]] = []
    for item in _field(response, "data", []) or []:
        image_base64 = _field(item, "b64_json") or _field(item, "result") or ""
        revised_prompt = _field(item, "revised_prompt") or ""
        if image_base64:
            image_responses.append((str(image_base64), str(revised_prompt or "")))
    return image_responses


def _extract_image_response(response: Any) -> tuple[str, str]:
    for image_base64, revised_prompt in _extract_image_responses(response):
        return image_base64, revised_prompt
    return "", ""


def _extract_minimax_base64_images(response: Any) -> list[str]:
    data = _field(response, "data", {}) or {}
    raw_images = _field(data, "image_base64") or _field(data, "images") or []
    if isinstance(raw_images, str):
        return [raw_images]
    if not isinstance(raw_images, list):
        return []
    images: list[str] = []
    for item in raw_images:
        if isinstance(item, str):
            images.append(item)
        else:
            image_base64 = _field(item, "b64_json") or _field(item, "image_base64") or _field(item, "base64")
            if image_base64:
                images.append(str(image_base64))
    return images


def _extract_qwen_image_urls(response: Any) -> list[str]:
    output = _field(response, "output", {}) or {}
    image_urls: list[str] = []

    for choice in _field(output, "choices", []) or []:
        message = _field(choice, "message", {}) or {}
        for item in _field(message, "content", []) or []:
            image_url = _field(item, "image") or _field(item, "url")
            if image_url:
                image_urls.append(str(image_url))

    for item in _field(output, "results", []) or []:
        image_url = _field(item, "url") or _field(item, "image")
        if image_url:
            image_urls.append(str(image_url))

    return image_urls


def _extract_qwen_revised_prompt(response: Any) -> str:
    output = _field(response, "output", {}) or {}
    for item in _field(output, "results", []) or []:
        actual_prompt = _field(item, "actual_prompt")
        if actual_prompt:
            return str(actual_prompt)
    return ""


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
    raise ValueError("output_format must be one of: png, jpeg, webp")


def _normalize_openai_size(value: Any) -> str:
    normalized = str(value or DEFAULT_IMAGE_GENERATION_SIZE).strip().lower()
    allowed = {"auto", "1024x1024", "1024x1536", "1536x1024"}
    if normalized in allowed:
        return normalized
    raise ValueError("size must be one of: auto, 1024x1024, 1024x1536, 1536x1024")


def _normalize_openai_quality(value: Any) -> str:
    normalized = str(value or DEFAULT_IMAGE_GENERATION_QUALITY).strip().lower()
    allowed = {"auto", "low", "medium", "high"}
    if normalized in allowed:
        return normalized
    raise ValueError("quality must be one of: auto, low, medium, high")


def _normalize_openai_background(value: Any) -> Optional[str]:
    normalized = _clean_optional(value)
    if normalized is None:
        return None
    normalized = normalized.lower()
    allowed = {"auto", "opaque", "transparent"}
    if normalized in allowed:
        return normalized
    raise ValueError("background must be one of: auto, opaque, transparent")


def _normalize_openai_moderation(value: Any) -> Optional[str]:
    normalized = _clean_optional(value)
    if normalized is None:
        return None
    normalized = normalized.lower()
    allowed = {"auto", "low"}
    if normalized in allowed:
        return normalized
    raise ValueError("moderation must be one of: auto, low")


def _normalize_output_compression(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        compression = int(value)
    except (TypeError, ValueError):
        raise ValueError("output_compression must be an integer from 0 to 100")
    if compression < 0 or compression > 100:
        raise ValueError("output_compression must be an integer from 0 to 100")
    return compression


def _normalize_count(value: Any, *, max_count: int) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"n must be an integer from 1 to {max_count}")
    if count < 1 or count > max_count:
        raise ValueError(f"n must be an integer from 1 to {max_count}")
    return count


def _normalize_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_optional_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError("Boolean image generation options must be true or false")


def _normalize_minimax_aspect_ratio(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized in MINIMAX_IMAGE_GENERATION_ASPECT_RATIOS:
        return normalized
    allowed = ", ".join(sorted(MINIMAX_IMAGE_GENERATION_ASPECT_RATIOS))
    raise ValueError(f"aspect_ratio must be one of: {allowed}")


def _minimax_aspect_ratio_from_size(value: Any) -> Optional[str]:
    size = str(value or "").strip().lower()
    if not size or size == "auto":
        return None
    if "x" not in size:
        raise ValueError("size must be auto or WIDTHxHEIGHT")
    width_text, height_text = size.split("x", 1)
    try:
        width = int(width_text.strip())
        height = int(height_text.strip())
    except ValueError:
        raise ValueError("size must be auto or WIDTHxHEIGHT")
    if width <= 0 or height <= 0:
        raise ValueError("size width and height must be positive")
    common_divisor = _greatest_common_divisor(width, height)
    ratio = f"{width // common_divisor}:{height // common_divisor}"
    if ratio in MINIMAX_IMAGE_GENERATION_ASPECT_RATIOS:
        return ratio
    allowed = ", ".join(sorted(MINIMAX_IMAGE_GENERATION_ASPECT_RATIOS))
    raise ValueError(f"size aspect ratio must resolve to one of: {allowed}")


def _greatest_common_divisor(left: int, right: int) -> int:
    while right:
        left, right = right, left % right
    return left


def _normalize_minimax_dimension(value: Any) -> Optional[int]:
    dimension = _normalize_int(value)
    if dimension is None:
        return None
    if dimension < 512 or dimension > 2048 or dimension % 8 != 0:
        raise ValueError("MiniMax width/height must be integers from 512 to 2048 and multiples of 8")
    return dimension


def _normalize_qwen_size(value: Any) -> str:
    normalized = str(value or DEFAULT_QWEN_IMAGE_GENERATION_SIZE).strip().lower()
    if normalized == "auto":
        return DEFAULT_QWEN_IMAGE_GENERATION_SIZE
    normalized = normalized.replace("x", "*")
    if "*" not in normalized:
        raise ValueError("Qwen size must be auto or WIDTH*HEIGHT")
    width_text, height_text = normalized.split("*", 1)
    try:
        width = int(width_text.strip())
        height = int(height_text.strip())
    except ValueError:
        raise ValueError("Qwen size must be auto or WIDTH*HEIGHT")
    if width <= 0 or height <= 0:
        raise ValueError("Qwen size width and height must be positive")
    total_pixels = width * height
    if total_pixels < 512 * 512 or total_pixels > 2048 * 2048:
        raise ValueError("Qwen size total pixels must be between 512*512 and 2048*2048")
    return f"{width}*{height}"


def _normalize_qwen_count(value: Any, *, model: str) -> Optional[int]:
    max_count = 6 if _is_qwen_2_model(model) else 1
    count = _normalize_count(value, max_count=max_count)
    if count is not None and max_count == 1 and count != 1:
        raise ValueError("n must be 1 for qwen-image, qwen-image-plus, and qwen-image-max models")
    return count


def _normalize_qwen_seed(value: Any) -> Optional[int]:
    seed = _normalize_int(value)
    if seed is None:
        return None
    if seed < 0 or seed > 2147483647:
        raise ValueError("seed must be an integer from 0 to 2147483647")
    return seed


def _normalize_reference_image_urls(*values: Any) -> list[str]:
    urls: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                urls.append(text)
            continue
        if isinstance(value, list):
            for item in value:
                item_text = str(item or "").strip()
                if item_text:
                    urls.append(item_text)
    return urls


def _get_minimax_api_key(config: dict) -> str:
    configured_key = str(config.get("api_key") or "").strip()
    if configured_key and not _is_placeholder_api_key(configured_key):
        return configured_key
    for env_name in MINIMAX_API_KEY_ENV_VARS:
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            return env_value
    return ""


def _get_qwen_api_key(config: dict) -> str:
    configured_key = str(config.get("api_key") or "").strip()
    if configured_key and not _is_placeholder_api_key(configured_key):
        return configured_key
    for env_name in QWEN_API_KEY_ENV_VARS:
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            return env_value
    return ""


def _is_placeholder_api_key(api_key: str) -> bool:
    normalized = api_key.strip().lower()
    return not normalized or normalized in PLACEHOLDER_API_KEYS


def _minimax_image_generation_endpoint(config: dict) -> str:
    configured_endpoint = str(config.get("endpoint") or config.get("base_url") or "").strip().rstrip("/")
    if not configured_endpoint:
        return MINIMAX_IMAGE_GENERATION_ENDPOINT
    if configured_endpoint.endswith("/image_generation"):
        return configured_endpoint
    if configured_endpoint.endswith("/v1"):
        return f"{configured_endpoint}/image_generation"
    return f"{configured_endpoint}/v1/image_generation"


def _qwen_image_generation_endpoint(config: dict) -> str:
    configured_endpoint = str(config.get("endpoint") or config.get("base_url") or "").strip().rstrip("/")
    if not configured_endpoint:
        return QWEN_IMAGE_GENERATION_ENDPOINT
    if configured_endpoint.endswith("/services/aigc/multimodal-generation/generation"):
        return configured_endpoint
    if configured_endpoint.endswith("/compatible-mode/v1"):
        root = configured_endpoint[: -len("/compatible-mode/v1")]
        return f"{root}/api/v1/services/aigc/multimodal-generation/generation"
    if configured_endpoint.endswith("/api/v1"):
        return f"{configured_endpoint}/services/aigc/multimodal-generation/generation"
    return f"{configured_endpoint}/api/v1/services/aigc/multimodal-generation/generation"


def _is_gpt_image_2(model: str) -> bool:
    return model.strip().lower() == "gpt-image-2"


def _is_qwen_2_model(model: str) -> bool:
    return model.strip().lower().startswith("qwen-image-2.0")


def _detect_image_format(image_bytes: bytes, *, fallback: str) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "webp"
    return fallback


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
