"""xAgent global defaults — single source of truth for all magic numbers."""

# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

DEFAULT_HISTORY_COUNT = 16       # Messages included in each model call
DEFAULT_MAX_ITER = 10            # Maximum reasoning/tool-call iterations

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

DEFAULT_MAX_CONCURRENT_TOOLS = 10   # Semaphore limit for parallel tool calls
TOOL_RESULT_PREVIEW_LENGTH = 200    # Characters shown in history content field
TOOL_RESULT_PREVIEW_LENGTH_VERBOSE = 1000  # Preview length in verbose/debug mode
ERROR_RESPONSE_PREVIEW_LENGTH = 200  # Characters shown from HTTP error bodies

# ---------------------------------------------------------------------------
# MCP cache
# ---------------------------------------------------------------------------

MCP_CACHE_TTL = 300              # Seconds before MCP tool list is re-fetched

# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

HTTP_TIMEOUT = 600.0             # Seconds before sub-agent HTTP request times out

# ---------------------------------------------------------------------------
# Retry / background tasks
# ---------------------------------------------------------------------------

RETRY_ATTEMPTS = 3
RETRY_MIN_WAIT = 1               # Seconds (exponential backoff minimum)
RETRY_MAX_WAIT = 60              # Seconds (exponential backoff maximum)
BACKGROUND_TASK_ATTEMPTS = 3
BACKGROUND_TASK_BASE_DELAY = 0.5  # Seconds, multiplied by attempt number
DEFAULT_MAX_BACKGROUND_TASKS = 4  # Semaphore limit for background tasks

# ---------------------------------------------------------------------------
# Memory system
# ---------------------------------------------------------------------------

MEMORY_BUFFER_THRESHOLD = 10    # Messages before flushing to long-term storage
MEMORY_KEEP_RECENT = 2          # Messages retained in buffer after flush
MEMORY_TTL_SECONDS = 2592000    # 30 days — long-term memory entry TTL
MEMORY_RETRIEVAL_LIMIT = 5      # Default k for memory vector search
LOCAL_BUFFER_MAX_SIZE = 100     # Maximum messages kept in local buffer

# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

WORKFLOW_MAX_CONCURRENT = 10    # Semaphore limit for parallel workflow nodes

# ---------------------------------------------------------------------------
# Agent defaults
# ---------------------------------------------------------------------------

DEFAULT_AGENT_NAME = "default_agent"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_USER_ID = "default_user"
DEFAULT_SESSION_ID = "default_session"

# ---------------------------------------------------------------------------
# Image captioning
# ---------------------------------------------------------------------------

IMAGE_CAPTION_MODEL = "gpt-4o-mini"
IMAGE_CAPTION_PROMPT = (
    "Describe this image in detail for future reference. Include: subject matter, "
    "composition, colors, style, mood, and any notable details. Be concise but thorough. "
    "Respond in the same language as the user's original prompt if provided."
)

# ---------------------------------------------------------------------------
# API parameter limits (DoS prevention)
# ---------------------------------------------------------------------------

API_MAX_ITER_LIMIT = 50          # Hard upper bound on max_iter from API clients
API_MAX_CONCURRENT_TOOLS_LIMIT = 20  # Hard upper bound on max_concurrent_tools
API_MAX_HISTORY_COUNT_LIMIT = 100    # Hard upper bound on history_count
