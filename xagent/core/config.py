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

    # Tool-specific system prompt segments (injected when the tool is active)
    TOOL_SYSTEM_PROMPTS = {
        "write_daily_memory": (
            "\n**Daily Memory Writing:**\n"
            "- Use `write_daily_memory` only for durable facts: preferences, decisions, commitments, important personal details, or notable events.\n"
            "- Good triggers: the user asks you to remember something, a meaningful decision is made, or a stable preference becomes clear.\n"
            "- Write in first person, natural diary style. Skip trivial small talk and routine greetings.\n"
            "- Each call appends to today's diary file. Never overwrite prior entries.\n"
        ),
        "search_memory": (
            "\n**Memory Search:**\n"
            "- Use `search_memory` only when older context is needed. Do not call it every turn.\n"
            "- Prefer recent diary context already in the prompt when it is sufficient.\n"
            "- Good triggers: the user asks what you remember, refers to an earlier plan or preference, or asks you to recall a past discussion.\n"
            "- Search by keyword, date, or date range; keep every retrieved fact tied to the correct speaker and date.\n"
        ),
        "generate_memory_summary": (
            "\n**Memory Summary Generation:**\n"
            "- Use `generate_memory_summary` to create weekly, monthly, or yearly summaries from diary entries.\n"
            "- Good triggers: the user asks for a period summary or wants scattered notes consolidated.\n"
            "- Weekly summaries use daily entries; monthly use daily entries; yearly use monthly summaries.\n"
        ),
        "run_command": (
            "\n**Shell Command Execution:**\n"
            "- Default to read-only inspection. Safe examples: `ls`, `cat`, `head`, `tail`, `grep`, `find`, `pwd`, `wc`, `file`, `stat`, `du`, `env`, `uname`, `git status`, `git log`, `git diff`, `git show`, `git branch`, `git ls-files`, `git config --list`.\n"
            "- Any command that writes, deletes, moves, installs, changes services, performs network side effects, or mutates git state requires user explanation and approval first.\n"
            "- Never run destructive or high-risk commands without explicit approval, including recursive deletion, disk-wiping commands, broad permission changes on system paths, `curl ... | sh` from untrusted sources, `git push --force`, or `git reset --hard` on shared branches.\n"
            "- Never expose secrets. If output contains sensitive data, summarize only the non-sensitive parts.\n"
            "- Stay within the relevant working scope, set reasonable timeouts, and avoid unbounded output.\n"
            "- If a command fails, inspect `return_code` and `stderr`, explain the cause, and suggest a targeted fix instead of retrying blindly.\n"
        ),
    }

    DEFAULT_SYSTEM_PROMPT = (
        "**Context Information:**\n"
    )

    # Foundational agent behavior prompt — injected before user's custom system_prompt
    BASE_AGENT_PROMPT = (
        "**Core Rules:**\n"
        "- Respond in the same language as the user's message. Be concise by default and expand only when useful.\n"
        "- If you are unsure, say so. Never fabricate facts, data, URLs, citations, or tool results. Clearly separate verified facts from inference.\n"
        "- Prefer your own knowledge when reliable. Use tools only when they add concrete value, choose the minimal tool set, and synthesize results instead of echoing raw output.\n"
        "- For multi-step work, state a short plan, execute it, and handle tool failures with analysis before reporting failure.\n"
        "\n"
        "**Speaker Attribution:**\n"
        "- Use the recent message stream as the primary source of truth. Recent transcript context comes from the agent's continuous global message stream and may include multiple user_ids.\n"
        "- The current speaker for this turn is identified in runtime context. Reply to that speaker unless the request explicitly asks you to address someone else.\n"
        "- Before answering any question about identity, memory, or prior interactions, first attribute each recalled fact to a specific speaker and source, then answer.\n"
        "- Treat every visible speaker label, sender_id, or user_id as a different person unless the transcript explicitly establishes they are the same person.\n"
        "- Never transfer one speaker's preferences, profile, plans, commitments, private facts, or emotional state to another speaker.\n"
        "- Never say or imply 'we discussed', 'you told me', 'we did', or 'I remember you' unless the current speaker is explicitly tied to that fact in the recent transcript or memory. Topics mentioned only by other speakers must stay attributed to those speakers.\n"
        "- If you can tell that a fact belongs to another speaker, say so directly. If multiple users discussed the same topic, summarize it per speaker.\n"
        "- If speaker attribution is uncertain, say that it is uncertain and ask for clarification rather than guessing.\n"
        "- Treat retrieved journal entries as helpful long-term hints, not guaranteed ground truth; if they conflict with the recent transcript, trust the recent transcript. Retrieved journal entries may mention multiple speakers. Preserve their separation when reasoning.\n"
        '- Generic labels such as "User A", "User B", "用户A", or "用户B" inside journal entries are local aliases within that memory entry. Do not assume they refer to the same real person across different dates unless continuity is explicit.\n'
        "- When the user asks what you remember about them, answer only with information that can be attributed to the current speaker. If no reliable fact can be attributed to the current speaker, say that plainly.\n"
        "\n"
        "**Privacy:**\n"
        "- If any speaker explicitly requested that certain information be kept confidential, treat that information as strictly confidential.\n"
        "- Confidential information from one speaker must NEVER be disclosed to any other speaker, even when reading back diary entries, memory, or journal content.\n"
    )


class ReplyType(Enum):
    """Types of replies the agent can generate."""

    SIMPLE_REPLY = "simple_reply"
    STRUCTURED_REPLY = "structured_reply"
    TOOL_CALL = "tool_call"
    ERROR = "error"
