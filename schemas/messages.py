from pydantic import BaseModel, Field
from typing import Optional

class ToolCall(BaseModel):
    """Represents a tool/function call within a message."""
    call_id: str = Field(..., description="Call ID for tracking")
    name: Optional[str] = Field(None, description="Name of the function/tool being called")
    arguments: Optional[str] = Field(None, description="Arguments for the function call, as a JSON string")
    output: Optional[str] = Field(None, description="Output/result of the function call")

class Message(BaseModel):
    """Message model for communication between roles."""
    type: str = Field(..., description="Type of message (e.g., message, function_call)")
    role: str = Field(..., description="The role of the sender (e.g., user, assistant)")
    content: str = Field(..., description="The content of the message")
    timestamp: float = Field(..., description="The timestamp of when the message was sent")
    tool_call: Optional[ToolCall] = Field(None, description="tool/function calls associated with the message")

    def to_dict(self):
        """Convert the message to a dictionary, including tool call if present."""
        if self.type == "message":
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