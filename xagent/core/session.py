"""Session naming utilities shared by agent interfaces and storage operations."""


def normalize_agent_name(name: str) -> str:
    """Convert an agent name into a stable session prefix."""
    return (name or "default_agent").lower().replace(" ", "_").replace("-", "_")


def normalize_session_id(agent_name: str, session_id: str) -> str:
    """Apply the agent-scoped session namespace used by the runtime."""
    return f"{normalize_agent_name(agent_name)}:{session_id}"
