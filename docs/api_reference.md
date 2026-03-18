# API Reference

## Agent

`Agent` is the main runtime entry point.

```python
Agent(
    name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    description: Optional[str] = None,
    model: Optional[str] = None,
    client: Optional[AsyncOpenAI] = None,
    tools: Optional[list] = None,
    mcp_servers: Optional[str | list[str]] = None,
    output_type: Optional[type[BaseModel]] = None,
    message_storage: Optional[MessageStorageBase] = None,
    memory_storage: Optional[MemoryStorageBase] = None,
    workspace: Optional[str] = None,
)
```

### Chat API

```python
await agent.chat(
    user_message: str,
    user_id: str = "default_user",
    history_count: int = 100,
    max_iter: int = 10,
    max_concurrent_tools: int = 10,
    image_source: str | list[str] | None = None,
    output_type: type[BaseModel] | None = None,
    stream: bool = False,
    enable_memory: bool = True,
)
```

### Chat Parameters

| Parameter | Type | Description |
|---|---|---|
| `user_message` | string | Current speaker message |
| `user_id` | string | Current speaker identifier |
| `history_count` | integer | Number of messages loaded from storage |
| `max_iter` | integer | Maximum model-call loop count |
| `max_concurrent_tools` | integer | Maximum parallel tool calls |
| `image_source` | string or list | Image URL, path, or data URI |
| `output_type` | Pydantic model type | Structured output model |
| `stream` | boolean | Enable streaming |
| `enable_memory` | boolean | Enable long-term memory retrieval and writes. Defaults to `true`. |

### Message Stream Behavior

- The agent keeps one continuous message stream
- `user_id` always means the current speaker
- Speaker identifiers are included in user messages sent to the model
- Recent context is pulled from the global recent-message window for that agent

### Example

```python
from xagent.core import Agent

agent = Agent(name="assistant", model="gpt-5-mini")

reply = await agent.chat(
    user_message="Hello",
    user_id="alice",
)

follow_up = await agent.chat(
    user_message="Summarize what this conversation has decided.",
    user_id="bob",
)
```

## AgentHTTPServer

```python
AgentHTTPServer(
    config_path: Optional[str] = None,
    toolkit_path: Optional[str] = None,
    agent: Optional[Agent] = None,
    enable_web: bool = True,
)
```

### Main Endpoints

- `GET /health`
- `POST /chat`
- `POST /clear_messages`
- `GET /memory`

### `POST /chat`

Request body:

```json
{
  "user_id": "alice",
  "user_message": "Hello",
  "stream": false,
  "enable_memory": true
}
```

### `POST /clear_messages`

This endpoint clears the agent's entire message stream.

### `GET /memory`

Query parameters:

- `query`
- `date`
- `limit`

## Message Storage

### MessageStorageBase

Base interface for single-stream message storage.

```python
async def add_messages(messages, **kwargs) -> None
async def get_messages(count: int = 100) -> list[Message]
async def clear_messages() -> None
async def pop_message() -> Message | None
async def get_message_count() -> int
async def has_messages() -> bool
def get_stream_info() -> dict[str, str]
```

### MessageStorageLocal

SQLite-backed local message storage.

```python
from xagent.components import MessageStorageLocal

storage = MessageStorageLocal()
agent = Agent(message_storage=storage)
```

Custom backends can implement `MessageStorageBase` and be injected into `Agent`.

## Memory Storage

### MemoryStorageBase

```python
async def add(memory_key: str, messages: list[dict]) -> None
async def store(memory_key: str, content: str) -> str | None
async def retrieve(
    memory_key: str,
    query: str = "",
    limit: int = 5,
    journal_date: str | None = None,
) -> list | None
async def clear(memory_key: str) -> None
async def delete(memory_ids: list[str]) -> None
```

### Memory Behavior

- Runtime memory is agent-scoped
- The agent's full message stream contributes to one per-agent daily journal
- Journal entries are stored in the same SQLite database file as messages
- Retrieval supports exact-date lookups, keyword search, and date-filtered keyword search
- Custom backends can implement `MemoryStorageBase` or reuse `MemoryStorageBasic`

## Tools

Built-in tools:

- `web_search`
- `draw_image`
- `run_command`

### `function_tool`

```python
from xagent.utils import function_tool


@function_tool()
def your_function(arg: str) -> str:
    return arg
```

Use decorated functions in `Agent(tools=[...])`.
