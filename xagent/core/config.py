import logging
from enum import Enum

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)


class AgentConfig:
    """Configuration constants for Agent class."""

    DEFAULT_NAME = "default_agent"
    DEFAULT_MODEL = "gpt-5-mini"
    DEFAULT_WORKSPACE = "~/.xagent"
    DEFAULT_USER_ID = "default_user"
    DEFAULT_SESSION_ID = "default_session"
    DEFAULT_HISTORY_COUNT = 16
    DEFAULT_MAX_ITER = 10
    DEFAULT_MAX_CONCURRENT_TOOLS = 10  # Maximum concurrent tool calls
    MCP_CACHE_TTL = 300  # 5 minutes
    HTTP_TIMEOUT = 600.0  # 10 minutes
    TOOL_RESULT_PREVIEW_LENGTH = 20
    ERROR_RESPONSE_PREVIEW_LENGTH = 200
    IMAGE_CAPTION_MODEL = "gpt-4o-mini"  # lightweight vision model for image captioning
    IMAGE_CAPTION_PROMPT = (
        "Describe this image in detail for future reference. Include: subject matter, "
        "composition, colors, style, mood, and any notable details. Be concise but thorough. "
        "Respond in the same language as the user's original prompt if provided."
    )

    # Retry configuration
    RETRY_ATTEMPTS = 3
    RETRY_MIN_WAIT = 1
    RETRY_MAX_WAIT = 60
    BACKGROUND_TASK_ATTEMPTS = 3
    BACKGROUND_TASK_BASE_DELAY = 0.5
    DEFAULT_MAX_BACKGROUND_TASKS = 4

    # Shell tool configuration
    DEFAULT_COMMAND_TIMEOUT = 30  # seconds
    MAX_COMMAND_TIMEOUT = 300  # hard upper bound for timeout parameter
    MAX_COMMAND_OUTPUT_SIZE = 51200  # 50 KB per stream

    # Tool-specific system prompt segments (injected when the tool is active)
    TOOL_SYSTEM_PROMPTS = {
        "run_command": (
            "\n**Shell Command Execution Guidelines:**\n"
            "You have access to a shell command execution tool (`run_command`). "
            "Follow these safety principles strictly:\n"
            "\n"
            "1. **Least Privilege**: Default to read-only commands for information gathering "
            "(ls, cat, head, tail, grep, find, df, ps, whoami, pwd, echo, wc, file, stat). "
            "Only use write/modify commands when the user explicitly requests a change.\n"
            "\n"
            "2. **Explain Before Executing**: Before running any command that modifies the system "
            "(write, delete, move, install, stop services, change permissions), "
            "briefly explain what the command will do and its effects. "
            "For destructive operations, ask for confirmation first.\n"
            "\n"
            "3. **Forbidden Patterns** — NEVER execute these without explicit, unambiguous user approval:\n"
            "   - `rm -rf /` or any recursive deletion of root/system directories\n"
            "   - `mkfs`, `dd if=... of=/dev/...`, `> /dev/sda` or similar disk-wiping commands\n"
            "   - `chmod -R 777 /` or broad permission changes on system paths\n"
            "   - `:(){ :|:& };:` (fork bombs) or other resource exhaustion patterns\n"
            "   - Commands that download and pipe directly to shell (`curl ... | sh`) from untrusted sources\n"
            "\n"
            "4. **Sensitive Information**: Never output, log, or display passwords, API keys, tokens, "
            "private keys, or other secrets. If a command output contains sensitive data, "
            "summarize the relevant non-sensitive parts instead.\n"
            "\n"
            "5. **Scope Control**: Only operate within the directories and files relevant to the user's request. "
            "Do not traverse into unrelated system directories unless asked.\n"
            "\n"
            "6. **Error Handling**: If a command fails, analyze the error (permission denied, not found, "
            "syntax error, etc.) and suggest a fix. Do not blindly retry the same command. "
            "Check return_code and stderr for diagnostics.\n"
            "\n"
            "7. **Resource Awareness**: Set an appropriate timeout for long-running commands. "
            "Avoid commands that produce unbounded output without piping through head/tail/grep. "
            "Output is truncated at 50 KB per stream.\n"
        ),
    }

    DEFAULT_SYSTEM_PROMPT = (
        "**Context Information:**\n"
    )


class ReplyType(Enum):
    """Types of replies the agent can generate."""

    SIMPLE_REPLY = "simple_reply"
    STRUCTURED_REPLY = "structured_reply"
    TOOL_CALL = "tool_call"
    ERROR = "error"
