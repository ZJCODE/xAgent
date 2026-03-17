"""Conversation naming utilities shared by agent interfaces and storage operations."""


def normalize_agent_name(name: str) -> str:
    """Convert an agent name into a stable conversation prefix."""
    return (name or "default_agent").lower().replace(" ", "_").replace("-", "_")


def normalize_conversation_id(agent_name: str, conversation_id: str) -> str:
    """Apply the agent-scoped conversation namespace used by the runtime."""
    return f"{normalize_agent_name(agent_name)}:{conversation_id}"
