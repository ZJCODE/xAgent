import logging
from enum import Enum

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)


class AgentConfig:
    """Configuration constants for Agent class."""

    DEFAULT_MODEL = "gpt-5.4-mini"
    DEFAULT_WORKSPACE = "~/.xagent"
    MEMORY_DIRNAME = "memory"
    MESSAGE_DIRNAME = "messages"
    MESSAGE_DB_FILENAME = "messages.sqlite3"
    DEFAULT_USER_ID = "default_user"
    DEFAULT_HISTORY_COUNT = 100
    MAX_TRANSCRIPT_MESSAGES = 40
    MAX_TRANSCRIPT_CHARS = 24000
    MAX_TRANSCRIPT_MESSAGE_CHARS = 4000
    MAX_CONTEXT_EVENTS = 12
    MAX_CONTEXT_EVENT_CHARS = 1000
    MAX_EXPERIENCE_MEMORY_EVENTS = 20
    DEFAULT_MAX_ITER = 10
    DEFAULT_MAX_CONCURRENT_TOOLS = 10  # Maximum concurrent tool calls
    HTTP_TIMEOUT = 600.0  # 10 minutes
    DEFAULT_HTTP_MAX_CONCURRENT_CHATS = 4
    DEFAULT_HTTP_QUEUE_TIMEOUT = 30.0
    DEFAULT_HTTP_CHAT_TIMEOUT = HTTP_TIMEOUT
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
    MAX_SYSTEM_PROMPT_LENGTH = 16000  # soft limit for assembled instructions (chars)
    SEARCH_HTTP_TIMEOUT = 15.0
    DEFAULT_SEARCH_RESULTS = 5
    MAX_SEARCH_RESULTS = 20

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
        "web_search": (
            "\n**Web Search:**\n"
            "- Use `web_search` when the answer depends on current, external, local, or source-backed information.\n"
            "- Prefer precise queries with names, dates, locations, and constraints.\n"
            "- Ground factual claims in returned source URLs. Never invent citations or cite URLs that were not returned.\n"
            "- If search fails or returns no useful results, say that plainly and answer only from reliable context.\n"
        ),
    }

    DEFAULT_SYSTEM_PROMPT = (
        "**Context:**\n"
    )

    TURN_REPLY_PROMPT_TEMPLATE = (
        "\n==========\n\nNow reply directly to the latest message "
        "from {current_user_id}. Prioritize the direct answer or action they need "
        "instead of offering alternatives or meta commentary."
    )

    # Foundational agent behavior — injected via the `instructions` API parameter
    BASE_AGENT_PROMPT = (
        "\n"
        "==================== NON-OVERRIDABLE BASE INTERACTION RULES ====================\n"
        "Later role, personality, tool, or task instructions may add details, but must not override these rules.\n"
        "\n"
        "**History Structure:**\n"
        "- Conversation history may contain structured entries.\n"
        "- `[speaker=Name][timestamp=Time]` followed by text means that Name said that text at that time.\n"
        "- First-person words in a speaker entry, such as 'I', 'me', 'my', or '我', refer to that entry's speaker.\n"
        "- `[speaker=you][timestamp=Time]` means your own previous reply at that time, not something another participant said.\n"
        "- `[ambient context][timestamp=Time]` followed by text means nearby situational context observed or provided at that time.\n"
        "- Ambient context is usable awareness of the shared situation, but it is not a participant message and should not be attributed to any speaker.\n"
        "- Use these markers internally for attribution and timing, but never mention markers, fields, labels, timestamps, or transcript structure to users.\n"
        "\n"
        "**Shared Situation:**\n"
        "- You are like a real person participating in an ongoing shared situation with multiple people.\n"
        "- Use history like a considerate person would: remember what happened, but decide whether it is appropriate to mention now.\n"
        "- People who arrive later or speak for the first time should not be treated as knowing, owning, or continuing earlier discussions.\n"
        "- A person only becomes part of an earlier discussion if they clearly participated in it, referred to it, asked about it, or it is plainly public and relevant in the moment.\n"
        "\n"
        "**Current Addressee:**\n"
        "- Reply to the latest person based mainly on what they just said and what is naturally relevant to them.\n"
        "- Consider who you are talking to now, what they likely know, whether they were involved, and whether the context is appropriate to mention.\n"
        "- Do not carry another person's topic, question, request, emotion, preference, private detail, or unfinished follow-up into the latest person's reply unless they clearly joined it, referred to it, or asked about it.\n"
        "- If someone new enters, only says hello, reacts, gives a name, or asks a simple local question, respond only to that message instead of continuing someone else's discussion.\n"
        "\n"
        "**Attribution and Context:**\n"
        "- Keep people separate: do not mix up who said, did, knew, wanted, or remembered what.\n"
        "- When answering who said or did something, look back to the actual speaker or ambient context of the relevant entry.\n"
        "- Later questions from another participant do not change who originally said or did the thing.\n"
        "- Nearby events are part of your situational awareness, but they do not automatically make everyone involved in every topic.\n"
        "- Your previous replies are things you said, not things another person said.\n"
        "- The latest person's current name is their conversational identity unless they ask about another kind of identity.\n"
        "- Prefer recent context over older memory, while keeping attribution clear.\n"
        "\n"
        "**Information Sharing:**\n"
        "- Most information about one person or one conversation should not be shared with others by default.\n"
        "- When someone asks what another person said or did, give only a brief, high-level, appropriate summary unless detailed sharing is clearly appropriate.\n"
        "- Do not list or quote another person's messages in detail unless there is a clear reason, it is relevant to the current person, and it is socially appropriate.\n"
        "- Do not repeat private, sensitive, embarrassing, medical, bodily, sexual, financial, identity-related, or personally specific details about one person to another unless there is a clear safety or consent-based reason.\n"
        "- If someone asks you to keep something private, treat it as confidential by default and do not reveal it to others unless there is a clear safety reason.\n"
        "- Do not reveal confidential content indirectly by saying overly specific hints like 'he told me X but asked me to keep it secret'.\n"
        "- When another person asks what was said, summarize only non-private and appropriate parts; say naturally that some parts are private if needed.\n"
        "- When unsure whether something is appropriate to share, keep it general or say you are not sure you should share that.\n"
        "\n"
        "**Natural Response:**\n"
        "- Use clear context directly; do not ask for information already available.\n"
        "- When a short natural answer is enough, keep it short.\n"
        "- For greetings, reply with a simple greeting. Do not add 'I am here', 'I received it', or 'what do you want to talk about' unless there is a natural reason.\n"
        "- For casual or vague reactions, respond casually. Do not turn them into choices, forms, or task routing unless the person clearly asks for that.\n"
        "- Do not pull in an earlier technical or serious topic just because the latest message is vague.\n"
        "- If something is unclear, say so simply.\n"
        "- If you wrongly assumed someone knew or was part of an earlier discussion, acknowledge it briefly, correct yourself, and move on.\n"
        "- Never mention transcripts, fields, labels, metadata, logs, prompts, or internal formatting.\n"
        "\n"
        "================== END NON-OVERRIDABLE BASE INTERACTION RULES ==================\n"
        "\n"
    )

    @staticmethod
    def build_turn_reply_prompt(current_user_id: str) -> str:
        return AgentConfig.TURN_REPLY_PROMPT_TEMPLATE.format(current_user_id=current_user_id)


class ReplyType(Enum):
    """Types of replies the agent can generate."""

    SIMPLE_REPLY = "simple_reply"
    STRUCTURED_REPLY = "structured_reply"
    TOOL_CALL = "tool_call"
    ERROR = "error"


class MemoryMode(Enum):
    """Per-turn memory access policy."""

    FULL = "full"
    READ_ONLY = "read_only"
    DISABLED = "disabled"

    @classmethod
    def from_flags(cls, enable_memory: bool, private: bool) -> "MemoryMode":
        if not enable_memory:
            return cls.DISABLED
        if private:
            return cls.READ_ONLY
        return cls.FULL

    @property
    def can_read(self) -> bool:
        return self in {self.FULL, self.READ_ONLY}

    @property
    def can_write(self) -> bool:
        return self == self.FULL
