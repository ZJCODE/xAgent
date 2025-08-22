import time
from pydantic import BaseModel, Field
from typing import Optional, Union, List
from enum import Enum

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
    image: Optional[Union[ImageContent, List[ImageContent]]] = Field(None, description="Image content associated with the message")
    voice: Optional[Union[VoiceContent, List[VoiceContent]]] = Field(None, description="Voice content associated with the message")
    document: Optional[Union[DocumentContent, List[DocumentContent]]] = Field(None, description="Document content associated with the message")

class MessageType(Enum):
    Message = "message"
    FUNCTION_CALL = "function_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"

class RoleType(Enum):
    """Enum for different roles in the system."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"

class Message(BaseModel):
    """Message model for communication between roles."""
    type: MessageType = Field(MessageType.Message, description="Type of message (e.g., message, function_call)")
    role: RoleType = Field(RoleType.USER, description="The role of the sender (e.g., user, assistant)")
    content: str = Field(..., description="The content of the message")
    timestamp: float = Field(default_factory=time.time, description="The timestamp of when the message was sent")
    tool_call: Optional[ToolCall] = Field(None, description="tool/function calls associated with the message")
    multimodal: Optional[MultiModalContent] = Field(None, description="Multi-modal content associated with the message")

    @classmethod
    def create(
        cls,
        content: str,
        role: Optional[RoleType] = RoleType.USER,
        image_source: Optional[Union[str, List[str]]] = None,
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
            # Handle single image or list of images
            sources = image_source if isinstance(image_source, list) else [image_source]
            image_contents = []
            
            for source in sources:
                processed_source = source
                if not (source.startswith("http") or source.startswith("data:image/")):
                    uploaded_url = upload_image(source)
                    if uploaded_url:
                        processed_source = uploaded_url
                    else:
                        raise ValueError(f"Image upload failed for source: {source}")
                
                image_contents.append(ImageContent(format="jpeg", source=processed_source))
            
            # Use single ImageContent if only one image, otherwise use list
            image_content = image_contents[0] if len(image_contents) == 1 else image_contents
            multimodal = MultiModalContent(image=image_content)

        return cls(
            role=role,
            type="message",
            content=content,
            multimodal=multimodal
        )

    def to_dict(self) -> dict:
        """Convert the message to a dictionary, including tool call if present."""
        if self.type == MessageType.Message:
            if self.multimodal and self.multimodal.image:
                content = [{"type": "input_text", "text": self.content}]
                
                # Handle single image or list of images
                images = self.multimodal.image if isinstance(self.multimodal.image, list) else [self.multimodal.image]
                for image in images:
                    content.append({
                        "type": "input_image",
                        "image_url": image.source,
                    })
                return {
                    "role": self.role.value,
                    "content": content,
                }
            return {
                "role": self.role.value,
                "content": self.content,
            }
        elif self.type in [MessageType.FUNCTION_CALL, MessageType.FUNCTION_CALL_OUTPUT]:
            result = {
            "call_id": self.tool_call.call_id,
            "type": self.type.value,
            "name": self.tool_call.name,
            "arguments": self.tool_call.arguments,
            "output": self.tool_call.output
            }
            # Filter out keys with value None
            return {k: v for k, v in result.items() if v is not None}
        else:
            raise ValueError(f"Unsupported message type: {self.type}")