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
    DEFAULT_HISTORY_COUNT = 100
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
    MAX_SYSTEM_PROMPT_LENGTH = 16000  # soft limit for assembled system prompt (chars)

    # Tool-specific system prompt segments (injected when the tool is active)
    TOOL_SYSTEM_PROMPTS = {
        "run_command": (
            "\n**Shell Command Execution Guidelines:**\n"
            "You have access to a shell command execution tool (`run_command`). "
            "Follow these safety principles strictly:\n"
            "\n"
            "**1. Read-Only by Default**\n"
            "The following commands may be executed freely for information gathering:\n"
            "- General: `ls`, `cat`, `head`, `tail`, `grep`, `find`, `df`, `ps`, "
            "`whoami`, `pwd`, `echo`, `wc`, `file`, `stat`, `tree`, `du`, `env`, `uname`\n"
            "- Git (read-only): `git status`, `git log`, `git diff`, `git branch`, "
            "`git show`, `git tag`, `git remote -v`, `git stash list`, `git blame`, "
            "`git shortlog`, `git rev-parse`, `git ls-files`, `git config --list`\n"
            "\n"
            "**2. User Confirmation Required**\n"
            "ALL commands that modify the system or repository MUST be explained and "
            "approved by the user before execution. This includes but is not limited to:\n"
            "- File operations: `rm`, `mv`, `cp`, `mkdir`, `touch`, `chmod`, `chown`, `ln`\n"
            "- Package management: `pip install`, `npm install`, `brew install`, `apt install`\n"
            "- Service control: `kill`, `systemctl`, `launchctl`\n"
            "- Git (write): `git add`, `git commit`, `git push`, `git pull`, `git merge`, "
            "`git rebase`, `git checkout`, `git switch`, `git reset`, `git revert`, "
            "`git stash drop`, `git stash pop`, `git branch -d`, `git tag -d`, "
            "`git cherry-pick`, `git clean`, `git submodule update`\n"
            "- Network: `curl -X POST/PUT/DELETE`, `wget`, `ssh`, `scp`, `rsync`\n"
            "- Any command that writes, deletes, moves, or modifies data\n"
            "Before running such commands, briefly state what the command does and "
            "wait for user approval.\n"
            "\n"
            "**3. Forbidden Patterns** — NEVER execute without explicit, unambiguous user approval:\n"
            "   - `rm -rf /` or any recursive deletion of root/system directories\n"
            "   - `mkfs`, `dd if=... of=/dev/...`, `> /dev/sda` or similar disk-wiping commands\n"
            "   - `chmod -R 777 /` or broad permission changes on system paths\n"
            "   - `:(){ :|:& };:` (fork bombs) or other resource exhaustion patterns\n"
            "   - Commands that download and pipe directly to shell (`curl ... | sh`) from untrusted sources\n"
            "   - `git push --force`, `git reset --hard` on shared branches\n"
            "\n"
            "**4. Sensitive Information**: Never output, log, or display passwords, API keys, tokens, "
            "private keys, or other secrets. If a command output contains sensitive data, "
            "summarize the relevant non-sensitive parts instead.\n"
            "\n"
            "**5. Scope Control**: Only operate within the directories and files relevant to the user's request. "
            "Do not traverse into unrelated system directories unless asked.\n"
            "\n"
            "**6. Error Handling**: If a command fails, analyze the error and suggest a fix. "
            "Do not blindly retry. Check return_code and stderr for diagnostics.\n"
            "\n"
            "**7. Resource Awareness**: Set an appropriate timeout for long-running commands. "
            "Avoid commands that produce unbounded output without piping through head/tail/grep. "
            "Output is truncated at 50 KB per stream.\n"
        ),
    }

    DEFAULT_SYSTEM_PROMPT = (
        "**Context Information:**\n"
    )

    # Foundational agent behavior prompt — injected before user's custom system_prompt
    BASE_AGENT_PROMPT = (
        "**Core Principles:**\n"
        "- Respond in the same language as the user's message.\n"
        "- Be concise for straightforward questions; provide depth when the task demands it.\n"
        "- If you are unsure about something, say so honestly. "
        "Never fabricate facts, data, URLs, citations, or tool results.\n"
        "- Clearly distinguish between verified information and your own inferences or suggestions.\n"
        "\n"
        "**Tool Use Strategy:**\n"
        "- Prefer answering from your knowledge when confident. "
        "Use tools only when they provide concrete value "
        "(real-time data, computation, file operations, external lookups).\n"
        "- Think before acting: identify what information you need, "
        "then select the minimal set of tool calls to obtain it.\n"
        "- After receiving tool results, synthesize them into a clear answer — "
        "do not simply echo raw output back to the user.\n"
        "- If a tool call fails, analyze the error and try an alternative approach "
        "before reporting failure to the user.\n"
        "- When multiple tool calls are independent of each other, execute them in parallel.\n"
        "\n"
        "**Response Quality:**\n"
        "- For multi-step tasks, briefly outline your plan, then execute step by step.\n"
        "- Structure complex answers with headings, lists, or numbered steps for readability.\n"
        "- If a request is ambiguous, state your interpretation and proceed; "
        "only ask for clarification when critical information is missing.\n"
        "\n"
        "**Memory & Context:**\n"
        "- Use the recent message stream as the primary source of truth.\n"
        "- Recent transcript context comes from the agent's continuous global message stream and may include multiple user_ids.\n"
        "- Treat retrieved journal entries as long-term context; if they conflict with the recent transcript, trust the recent transcript.\n"
        "- Use retrieved journal entries to personalize responses and maintain continuity across sessions.\n"
        "- Treat retrieved journal entries as helpful hints, not guaranteed ground truth.\n"
        "- Reference relevant earlier messages in the stream; avoid repeating what the user already knows.\n"
    )


class ReplyType(Enum):
    """Types of replies the agent can generate."""

    SIMPLE_REPLY = "simple_reply"
    STRUCTURED_REPLY = "structured_reply"
    TOOL_CALL = "tool_call"
    ERROR = "error"
