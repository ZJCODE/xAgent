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

class ToolCall(BaseModel):
    """Represents a tool/function call within a message."""
    call_id: str = Field(..., description="Call ID for tracking")
    name: Optional[str] = Field(None, description="Name of the function/tool being called")
    arguments: Optional[str] = Field(None, description="Arguments for the function call, as a JSON string")
    output: Optional[str] = Field(None, description="Output/result of the function call")

class ImageContent(BaseModel):
    """Represents image content in a message."""
    format: str = Field(..., description="Image format (e.g., png, jpeg)")
    source: Optional[str] = Field(None, description="URL or base64 string of the image")

class VoiceContent(BaseModel):
    """Represents voice content in a message."""
    format: str = Field(..., description="Voice format (e.g., mp3, wav)")
    source: Optional[bytes] = Field(None, description="The binary content of the voice file")

class DocumentContent(BaseModel):
    """Represents document content in a message."""
    format: str = Field(..., description="Document format (e.g., pdf, docx)")
    source: Optional[bytes] = Field(None, description="The binary content of the document")

class MultiModalContent(BaseModel):
    """Represents multi-modal content in a message."""
    image: Optional[Union[ImageContent, List[ImageContent]]] = Field(None, description="Image content associated with the message")
    voice: Optional[Union[VoiceContent, List[VoiceContent]]] = Field(None, description="Voice content associated with the message")
    document: Optional[Union[DocumentContent, List[DocumentContent]]] = Field(None, description="Document content associated with the message")

class MessageType(Enum):
    Message = "message"
    CONTEXT_EVENT = "context_event"
    FUNCTION_CALL = "function_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"

class RoleType(Enum):
    """Enum for different roles in the system."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    ENVIRONMENT = "environment"


class ContextEventInput(BaseModel):
    """Input payload for non-direct environmental observations."""

    context: str = Field(..., description="Observed context or overheard content")
    source: str = Field("environment", description="Stable source of the observation")
    event_type: str = Field("observation", description="Category of observation")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional event metadata")


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
    type: MessageType = Field(MessageType.Message, description="Type of message (e.g., message, function_call)")
    role: RoleType = Field(RoleType.USER, description="The role of the sender (e.g., user, assistant)")
    sender_id: Optional[str] = Field(None, description="Stable identifier for the speaker in the agent message stream")
    content: str = Field(..., description="The content of the message")
    timestamp: float = Field(default_factory=time.time, description="The timestamp of when the message was sent")
    tool_call: Optional[ToolCall] = Field(None, description="tool/function calls associated with the message")
    multimodal: Optional[MultiModalContent] = Field(None, description="Multi-modal content associated with the message")
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
            content (str): The text content of the message.
            role (Optional[str]): The role of the sender (default is "user").
            image_source (Optional[Union[str, List[str]]]): The URL, file path, base64 string, or list of these for images to be included in the message.
        Returns:
            Message: An instance of the Message class with the provided content and optional image(s).

        Raises:
            ValueError: If image upload fails.

        Usage:
            # Create a text message
            msg = Message.create("Hello, world!")
            # Create a message with specific role
            msg = Message.create("Hello, world!", role="assistant")
            # Create a message with a single image URL
            msg = Message.create("Hello, world!", image_source="https://example.com/image.jpg")
            # Create a message with multiple images
            msg = Message.create("Hello, world!", image_source=["image1.jpg", "image2.jpg"])
        """
        multimodal = None
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
            
            # Use single ImageContent if only one image, otherwise use list
            if image_contents:
                image_content = image_contents[0] if len(image_contents) == 1 else image_contents
                multimodal = MultiModalContent(image=image_content)

        return cls(
            role=role,
            type=MessageType.Message,
            sender_id=sender_id,
            content=content,
            multimodal=multimodal,
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
            sender_id=None,
            content=content,
            metadata=event_metadata,
        )

    def to_dict(self) -> dict:
        """Convert the message to a storage-safe dictionary representation."""
        base = {
            "role": self.role.value,
            "sender_id": self.sender_id,
            "content": self.content,
            "metadata": self.metadata,
        }

        if self.type in {MessageType.Message, MessageType.CONTEXT_EVENT}:
            if self.type != MessageType.Message:
                base["type"] = self.type.value
            return base
        if self.type in [MessageType.FUNCTION_CALL, MessageType.FUNCTION_CALL_OUTPUT]:
            result = {
                "call_id": self.tool_call.call_id,
                "type": self.type.value,
                "name": self.tool_call.name,
                "arguments": self.tool_call.arguments,
                "output": self.tool_call.output,
            }
            # Filter out keys with value None
            return {k: v for k, v in result.items() if v is not None}
        raise ValueError(f"Unsupported message type: {self.type}")

    def to_model_input(self) -> dict:
        """Convert the message to a Chat Completions message."""
        if self.type == MessageType.Message:
            text_content = self.content
            if self.sender_id and self.role == RoleType.USER:
                text_content = f"[{self.sender_id}] {text_content}"

            if self.multimodal and self.multimodal.image:
                content = [{"type": "text", "text": text_content}]

                images = self.multimodal.image if isinstance(self.multimodal.image, list) else [self.multimodal.image]
                for image in images:
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
                "role": RoleType.SYSTEM.value,
                "content": self.content,
            }

        if self.type == MessageType.FUNCTION_CALL:
            return {
                "role": RoleType.ASSISTANT.value,
                "content": None,
                "tool_calls": [
                    {
                        "id": self.tool_call.call_id,
                        "type": "function",
                        "function": {
                            "name": self.tool_call.name,
                            "arguments": self.tool_call.arguments or "{}",
                        },
                    }
                ],
            }

        if self.type == MessageType.FUNCTION_CALL_OUTPUT:
            return {
                k: v
                for k, v in {
                    "role": RoleType.TOOL.value,
                    "tool_call_id": self.tool_call.call_id,
                    "content": self.tool_call.output,
                }.items()
                if v is not None
            }

        raise ValueError(f"Unsupported message type: {self.type}")
