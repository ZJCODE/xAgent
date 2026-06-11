import time
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from ..utils.image_utils import (
    MAX_IMAGES_PER_MESSAGE,
    classify_source,
    extract_source,
    file_to_data_uri,
    infer_format,
    ImageSourceType,
)


class ImageContent(BaseModel):
    """Represents image content in a message."""
    format: str = Field(..., description="Image format (e.g., png, jpeg)")
    source: Optional[str] = Field(None, description="URL or base64 string of the image")


class MessageType(Enum):
    MESSAGE = "message"
    CONTEXT_EVENT = "context_event"


class RoleType(Enum):
    """Enum for different roles in the system."""
    USER = "user"
    ASSISTANT = "assistant"
    ENVIRONMENT = "environment"


class AgentTurnResult(BaseModel):
    """Public result for an agent turn that may choose silence."""

    kind: str = Field(..., description="Turn kind, such as chat or observe")
    replied: bool = Field(..., description="Whether an assistant reply was produced")
    reply: Optional[str] = Field(None, description="Assistant reply when one was produced")
    event_id: Optional[float] = Field(None, description="Timestamp identity for the triggering event")
    event_type: Optional[str] = Field(None, description="Context event category")
    source: Optional[str] = Field(None, description="Context event source")


class Message(BaseModel):
    """Message model for communication between roles."""
    type: MessageType = Field(MessageType.MESSAGE, description="Type of message")
    role: RoleType = Field(RoleType.USER, description="The role of the sender")
    sender_id: Optional[str] = Field(None, description="Stable identifier for the speaker in the agent message stream")
    content: str = Field(..., description="The content of the message")
    timestamp: float = Field(default_factory=time.time, description="The timestamp of when the message was sent")
    images: Optional[List[ImageContent]] = Field(None, description="Image content associated with the message")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional message metadata")

    @classmethod
    def create(
        cls,
        content: str,
        role: Optional[RoleType] = RoleType.USER,
        image_source: Optional[Union[str, List[str]]] = None,
        sender_id: Optional[str] = None,
    ) -> "Message":
        """
        Create a message with optional image content.

        Args:
            content: The text content of the message.
            role: The role of the sender (default is "user").
            image_source: The URL, file path, base64 string, or list of these for images.
            sender_id: Stable identifier for the speaker.

        Returns:
            Message: An instance of the Message class.

        Raises:
            ValueError: If image upload fails or too many images are provided.
        """
        images = None
        if image_source:
            sources = image_source if isinstance(image_source, list) else [image_source]
            if len(sources) > MAX_IMAGES_PER_MESSAGE:
                raise ValueError(f"At most {MAX_IMAGES_PER_MESSAGE} images are allowed per message")
            image_contents: List[ImageContent] = []
            seen_images: set[str] = set()

            for source in sources:
                raw_source = extract_source(str(source or "")).strip()
                if not raw_source or raw_source in seen_images:
                    continue
                seen_images.add(raw_source)
                source_type = classify_source(raw_source)
                processed_source = file_to_data_uri(raw_source) if source_type == ImageSourceType.FILE else raw_source
                if not processed_source:
                    raise ValueError(f"Failed to convert image to data URI: {raw_source}")
                image_contents.append(ImageContent(format=infer_format(processed_source), source=processed_source))

            if image_contents:
                images = image_contents

        return cls(
            role=role,
            type=MessageType.MESSAGE,
            sender_id=sender_id,
            content=content,
            images=images,
        )

    @classmethod
    def create_context_event(
        cls,
        content: str,
        source: str = "environment",
        event_type: str = "observation",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "Message":
        """Create a persisted observation/context event."""
        event_metadata = dict(metadata or {})
        event_metadata.setdefault("source", source)
        event_metadata.setdefault("event_type", event_type)
        return cls(
            role=RoleType.ENVIRONMENT,
            type=MessageType.CONTEXT_EVENT,
            content=content,
            metadata=event_metadata,
        )

    def to_model_input(self) -> dict:
        """Convert the message to a Chat Completions message."""
        if self.type == MessageType.MESSAGE:
            text_content = self.content
            if self.sender_id and self.role == RoleType.USER:
                text_content = f"[{self.sender_id}] {text_content}"

            if self.images:
                content = [{"type": "text", "text": text_content}]
                for image in self.images:
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": image.source},
                    })
                return {
                    "role": self.role.value,
                    "content": content,
                }
            return {
                "role": self.role.value,
                "content": text_content,
            }

        if self.type == MessageType.CONTEXT_EVENT:
            return {
                "role": "system",
                "content": self.content,
            }

        raise ValueError(f"Unsupported message type: {self.type}")
