from pydantic import BaseModel, Field
from typing import Optional

class ToolCall(BaseModel):
    """Represents a tool/function call within a message."""
    type: str = Field(..., description="Type of the tool call, e.g., 'function_call'")
    id: Optional[str] = Field(None, description="Unique identifier for the tool call")
    call_id: str = Field(..., description="Call ID for tracking")
    name: Optional[str] = Field(None, description="Name of the function/tool being called")
    arguments: Optional[str] = Field(None, description="Arguments for the function call, as a JSON string")
    output: Optional[str] = Field(None, description="Output/result of the function call")

class Message(BaseModel):
    """Message model for communication between roles."""
    role: str = Field(..., description="The role of the sender (e.g., user, assistant)")
    content: str = Field(..., description="The content of the message")
    timestamp: float = Field(..., description="The timestamp of when the message was sent")
    tool_call: Optional[ToolCall] = Field(None, description="tool/function calls associated with the message")