import time
from pydantic import BaseModel, Field
from typing import Optional

from ..utils.image_upload import upload_image

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
    image: Optional[ImageContent] = Field(None, description="Image content associated with the message")
    voice: Optional[VoiceContent] = Field(None, description="Voice content associated with the message")
    document: Optional[DocumentContent] = Field(None, description="Document content associated with the message")

class Message(BaseModel):
    """Message model for communication between roles."""
    type: str = Field("message", description="Type of message (e.g., message, function_call)")
    role: str = Field(..., description="The role of the sender (e.g., user, assistant)")
    content: str = Field(..., description="The content of the message")
    timestamp: float = Field(default_factory=time.time, description="The timestamp of when the message was sent")
    tool_call: Optional[ToolCall] = Field(None, description="tool/function calls associated with the message")
    multimodal: Optional[MultiModalContent] = Field(None, description="Multi-modal content associated with the message")

    @classmethod
    def create(
        cls,
        content: str,
        role: Optional[str] = "user",
        image_source: Optional[str] = None,
    ) -> "Message":
        """
        Create a message with optional image content.
        Args:
            content (str): The text content of the message.
            role (Optional[str]): The role of the sender (default is "user").
            image_source (Optional[str]): The URL or file path or base64 string of the image to be included in the message.
        Returns:
            Message: An instance of the Message class with the provided content and optional image.

        Raises:
            ValueError: If both image_url and image_source are provided.

        Usage:
            # Create a text message
            msg = Message.create("Hello, world!")
            # Create a message with specific role
            msg = Message.create("Hello, world!", role="assistant")
            # Create a message with an image URL
            msg = Message.create("Hello, world!", image_source="https://example.com/image.jpg")
        """
        multimodal = None
        if image_source:
            if not (image_source.startswith("http") or image_source.startswith("data:image/")):
                uploaded_url = upload_image(image_source)
                if uploaded_url:
                    image_source = uploaded_url
                else:
                    raise ValueError("Image upload failed, please check the image source.")
            multimodal = MultiModalContent(
                image=ImageContent(format="jpeg", source=image_source)
            )

        return cls(
            role=role,
            type="message",
            content=content,
            multimodal=multimodal
        )

    def to_dict(self) -> dict:
        """Convert the message to a dictionary, including tool call if present."""
        if self.type == "message":
            if self.multimodal and self.multimodal.image:
                return {
                    "role": self.role,
                    "content": [
                        {"type": "input_text", "text": self.content},
                        {
                            "type": "input_image",
                            "image_url": self.multimodal.image.source,
                        },
                    ],
                }
            return {
                "role": self.role,
                "content": self.content,
            }
        elif self.type in ["function_call", "function_call_output"]:
            result = {
            "call_id": self.tool_call.call_id,
            "type": self.type,
            "name": self.tool_call.name,
            "arguments": self.tool_call.arguments,
            "output": self.tool_call.output
            }
            # Filter out keys with value None
            return {k: v for k, v in result.items() if v is not None}
        else:
            raise ValueError(f"Unsupported message type: {self.type}")