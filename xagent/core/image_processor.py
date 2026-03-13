"""
ImageProcessor — image caption generation via a vision model.

Responsibilities:
- Calling the vision model to describe images
- Graceful fallback when captioning fails
"""

from __future__ import annotations

import logging
from typing import Optional

from ..defaults import IMAGE_CAPTION_MODEL, IMAGE_CAPTION_PROMPT


class ImageProcessor:
    """Generates captions for images using a vision model."""

    def __init__(
        self,
        client,
        caption_model: str = IMAGE_CAPTION_MODEL,
        caption_prompt: str = IMAGE_CAPTION_PROMPT,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.client = client
        self.caption_model = caption_model
        self.caption_prompt = caption_prompt
        self.logger = logger or logging.getLogger(__name__)

    async def caption(self, image_data_uri: str, prompt_hint: str = "") -> str:
        """
        Generate a text description of an image using a vision model.

        Args:
            image_data_uri: A ``data:image/...;base64,...`` URI or remote URL.
            prompt_hint: The original generation prompt used as context.

        Returns:
            A text description, or a graceful fallback string on failure.
        """
        prompt = self.caption_prompt
        if prompt_hint:
            prompt += f'\n\nOriginal generation prompt: "{prompt_hint}"'

        try:
            response = await self.client.responses.create(
                model=self.caption_model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": image_data_uri},
                        ],
                    }
                ],
            )
            caption = getattr(response, "output_text", "") or ""
            if caption.strip():
                return caption.strip()
        except Exception as exc:
            self.logger.warning(
                "Image captioning failed, using prompt-based fallback: %s", exc
            )

        if prompt_hint:
            return f'Generated image based on prompt: "{prompt_hint}"'
        return "An image was generated and displayed to the user."
