from pydantic import BaseModel, Field

class Message(BaseModel):
    """Message model for communication between roles."""
    role: str = Field(..., description="The role of the sender (e.g., user, assistant)")
    content: str = Field(..., description="The content of the message")
    timestamp: float = Field(..., description="The timestamp of when the message was sent")
    is_tool_result: bool = Field(False, description="Indicates if the message is a tool result message")