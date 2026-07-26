import logging
from enum import Enum

# Configure logging
_log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=_log_format)


class AgentConfig:
    """Configuration constants for Agent class.

    Organized by concern. Each section groups related parameters so that
    tuning one aspect of the agent does not require scanning the whole file.
    """

    # ============================================================
    # 1. Context Layer Names
    # Ordered dictionary keys used by the message handler to assemble
    # the system prompt from multiple context layers.
    # ============================================================
    CORE_INTERACTION_RULES_NAME = "core_interaction_rules"
    IDENTITY_CONTEXT_NAME = "identity_context"
    RECENT_MEMORY_NAME = "recent_memory"
    WORKSPACE_CONTEXT_NAME = "workspace_context"
    SKILLS_CATALOG_NAME = "skills_catalog"
    RECENT_EXPERIENCE_NAME = "recent_experience"
    CURRENT_EVENT_NAME = "current_event"
    CURRENT_TASK_NAME = "current_task"
    DECISION_RULES_NAME = "participation_decision_rules"

    # ============================================================
    # 2. Storage & Directory Layout
    # Workspace root, runtime-data directory names, and the SQLite
    # filename. Changing these alters where the agent persists state.
    # ============================================================
    DEFAULT_WORKSPACE = "~/.xagent"
    MEMORY_DIRNAME = "memory"
    WORKSPACE_DIRNAME = "workspace"
    SKILLS_DIRNAME = "skills"

    # ============================================================
    # 3. Model & Agent Defaults
    # LLM selection, generation caps, user identity, and tool-call
    # parallelism. These are the most frequently tuned knobs.
    # ============================================================
    DEFAULT_MODEL = "gpt-5.4-mini"
    DEFAULT_MAX_TOKENS = 8192
    DEFAULT_USER_ID = "default_user"
    TOOL_RESULT_PREVIEW_LENGTH = 20  # characters shown in tool-result summaries

    # ============================================================
    # 4. Agent Runtime Bounds
    # Iteration cap, conversation history window, and context-event
    # limit. Prevent infinite loops and unbounded prompt growth.
    # ============================================================
    DEFAULT_MAX_ITER = 50
    DEFAULT_MAX_HISTORY = 32
    MAX_CONTEXT_EVENTS = 12

    # ============================================================
    # 5. Safety & Resource Limits
    # Hard upper bounds for shell commands and assembled prompts.
    # These exist to prevent runaway resource consumption.
    # ============================================================
    MAX_COMMAND_TIMEOUT = 300  # hard upper bound for timeout parameter (seconds)
    MAX_COMMAND_OUTPUT_SIZE = 51200  # 50 KB per stream
    MAX_SKILLS_CATALOG_CHARS = 8000  # max characters for injected skill catalog

    # ============================================================
    # 6. Retry & Reliability
    # Exponential backoff parameters for LLM API calls.
    # ============================================================
    RETRY_ATTEMPTS = 3
    RETRY_MIN_WAIT = 1  # seconds
    RETRY_MAX_WAIT = 60  # seconds

    # ============================================================
    # 7. Memory & History
    # Tune the size and overlap of the recent-memory window.
    # Override per agent via config.yaml: agent.memory_recent_days (0 disables injection).
    # MEMORY_RECENT_MAX_CHARS is an internal prompt-budget guard, not user config.
    # ============================================================
    MEMORY_RECENT_DAYS = 2
    MEMORY_RECENT_MAX_CHARS = 8000
    MEMORY_WINDOW_OVERLAP_RATIO = 0.2

    # ============================================================
    # 8. Search Tool Defaults
    # Result-count bounds for the web_search tool.
    # ============================================================
    DEFAULT_SEARCH_RESULTS = 5
    MAX_SEARCH_RESULTS = 20

    # ============================================================
    # 9. HTTP Server
    # Only relevant when running in server mode. Concurrency and
    # timeout controls for the HTTP API channel.
    # ============================================================
    DEFAULT_HTTP_MAX_CONCURRENT_CHATS = 4
    DEFAULT_HTTP_QUEUE_TIMEOUT = 30.0  # seconds
    DEFAULT_HTTP_CHAT_TIMEOUT = 600.0  # 10 minutes

    # ============================================================
    # 10. Runtime Heartbeat
    # Keepalive / liveness signal emitted by the agent loop.
    # ============================================================
    RUNTIME_HEARTBEAT_ENABLED = True
    RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 300

    # Idle diary timeout is checked by the heartbeat loop, so the practical
    # trigger granularity is bounded by RUNTIME_HEARTBEAT_INTERVAL_SECONDS.
    # Keep this at or above the heartbeat interval unless you explicitly want
    # coarse polling. Set to 0 to disable. 21600 means 6 hours, which is a 
    # reasonable default for capturing idle time without being too noisy.
    IDLE_DIARY_TIMEOUT_SECONDS = 21600  # 6 hours

    # ============================================================
    # 10b. Subconscious (潜意识)
    # Low-probability autonomous thought generation. The heartbeat
    # rolls the dice each tick; when subconscious fires the agent
    # generates an internal thought and decides whether to share it.
    # ============================================================
    # Probability of spontaneous thought per heartbeat tick.
    # 0=off, 1=very active. Suggested: 0.01~0.1
    SUBCONSCIOUS_ACTIVITY = 0.0
    SUBCONSCIOUS_QUIET_HOURS_START = 22  # 10 PM – no immediate sends after this
    SUBCONSCIOUS_QUIET_HOURS_END = 8    # 8 AM – resume immediate sends

# ================================================================
# Reply Type Enum
# Classifies each agent turn: plain text, tool call, or error.
# Kept in config.py because both agent.py and model handler import it.
# ================================================================

class ReplyType(Enum):
    """Types of replies the agent can generate."""

    SIMPLE_REPLY = "simple_reply"
    TOOL_CALL = "tool_call"
    ERROR = "error"
