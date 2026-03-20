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
    DEFAULT_MODEL = "gpt-5.4-mini"
    DEFAULT_WORKSPACE = "~/.xagent"
    DEFAULT_USER_ID = "default_user"
    DEFAULT_HISTORY_COUNT = 100
    DEFAULT_MAX_ITER = 10
    DEFAULT_MAX_CONCURRENT_TOOLS = 10  # Maximum concurrent tool calls
    MCP_CACHE_TTL = 300  # 5 minutes
    HTTP_TIMEOUT = 600.0  # 10 minutes
    TOOL_RESULT_PREVIEW_LENGTH = 20
    ERROR_RESPONSE_PREVIEW_LENGTH = 200
    IMAGE_CAPTION_MODEL = "gpt-5.4-mini"  # lightweight vision model for image captioning
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

    # Tool-specific instruction segments (injected when the tool is active)
    TOOL_SYSTEM_PROMPTS = {
        "write_daily_memory": (
            "\n**Daily Memory Writing:**\n"
            "- Use `write_daily_memory` only for durable facts: preferences, decisions, commitments, personal details, or notable events.\n"
            "- Good triggers: user asks to remember something, a meaningful decision is made, or a stable preference is clear.\n"
            "- Write in first person, natural diary style. Skip trivial small talk.\n"
            "- Each call appends to today's diary. Never overwrite prior entries.\n"
        ),
        "search_memory": (
            "\n**Memory Search:**\n"
            "- Use `search_memory` only when older context is needed. Do not call it every turn.\n"
            "- Prefer recent diary context already provided in the transcript when sufficient.\n"
            "- Good triggers: user asks what you remember, refers to an earlier plan, or asks to recall a past discussion.\n"
            "- Search by keyword, date, or date range. Keep retrieved facts tied to the correct speaker and date.\n"
        ),
        "generate_memory_summary": (
            "\n**Memory Summary Generation:**\n"
            "- Use `generate_memory_summary` to create weekly, monthly, or yearly summaries from diary entries.\n"
            "- Good triggers: user asks for a period summary or wants scattered notes consolidated.\n"
            "- Weekly summaries use daily entries; monthly use daily entries; yearly use monthly summaries.\n"
        ),
        "run_command": (
            "\n**Shell Command Execution:**\n"
            "- Default to read-only inspection. Safe examples: `ls`, `cat`, `head`, `tail`, `grep`, `find`, `pwd`, `wc`, `file`, `stat`, `du`, `env`, `uname`, `git status`, `git log`, `git diff`, `git show`, `git branch`, `git ls-files`, `git config --list`.\n"
            "- Write, delete, install, network, or git-mutation commands require explicit user approval first.\n"
            "- Never run destructive commands without approval: recursive deletion, disk wipes, broad permission changes, `curl|sh` from untrusted sources, `git push --force`, `git reset --hard` on shared branches.\n"
            "- Never expose secrets; summarize only non-sensitive parts of output.\n"
            "- Stay within scope, use reasonable timeouts, and avoid unbounded output.\n"
            "- On failure: inspect `return_code` and `stderr`, explain the cause, suggest a targeted fix.\n"
        ),
    }

    DEFAULT_SYSTEM_PROMPT = (
        "**Context Information:**\n"
    )

    # Foundational agent behavior — injected via the `instructions` API parameter
    BASE_AGENT_PROMPT = (
        "**Core Rules:**\n"
        "- Match the user's language. Be concise by default; elaborate only when useful.\n"
        "- Never fabricate facts, data, URLs, citations, or tool results. State uncertainty explicitly.\n"
        "- Prefer your own knowledge when reliable. Use tools only for concrete value; synthesize results instead of echoing raw output.\n"
        "\n"
        "**Speaker Attribution (Multi-User Stream):**\n"
        "- The conversation stream is shared across multiple users. The current speaker is identified per-turn in context metadata.\n"
        "- Treat every speaker label, sender_id, or user_id as a distinct person unless the transcript explicitly says otherwise.\n"
        "- Never transfer one speaker's preferences, plans, commitments, or private facts to another speaker. Topics from other speakers stay attributed to them.\n"
        "- Never say or imply 'we discussed', 'you told me', 'we did', or 'I remember you' unless the current speaker is explicitly tied to that fact in the transcript or memory.\n"
        "- If speaker attribution is uncertain, say so and ask — never guess.\n"
        "- Journal/memory entries are long-term hints, not ground truth. When they conflict with the recent transcript, trust the transcript. Preserve per-speaker separation.\n"
        '- Generic labels like "User A", "User B", "用户A", or "用户B" in journal entries are local aliases; do not assume cross-date continuity.\n'
        "- When asked what you remember, answer only with information attributed to the current speaker. If no reliable fact can be attributed to the current speaker, say so plainly.\n"
        "\n"
        "**Privacy:**\n"
        "- Information any speaker marked confidential must never be disclosed to other speakers, including when reading back diary or memory content.\n"
    )


class ReplyType(Enum):
    """Types of replies the agent can generate."""

    SIMPLE_REPLY = "simple_reply"
    STRUCTURED_REPLY = "structured_reply"
    TOOL_CALL = "tool_call"
    ERROR = "error"
