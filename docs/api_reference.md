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
    conversation_id: str = "default_conversation",
    history_count: int = 16,
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
| `conversation_id` | string | Transcript identifier |
| `history_count` | integer | Number of messages loaded from storage |
| `max_iter` | integer | Maximum model-call loop count |
| `max_concurrent_tools` | integer | Maximum parallel tool calls |
| `image_source` | string or list | Image URL, path, or data URI |
| `output_type` | Pydantic model type | Structured output model |
| `stream` | boolean | Enable streaming |
| `enable_memory` | boolean | Enable long-term memory retrieval and writes. Defaults to `true`. |

### Conversation Behavior

- One `conversation_id` maps to one transcript
- `user_id` always means the current speaker
- Speaker identifiers are included in user messages sent to the model
- Reuse the same `conversation_id` to continue the same transcript

### Example

```python
from xagent.core import Agent

agent = Agent(name="assistant", model="gpt-5-mini")

reply = await agent.chat(
    user_message="Hello",
    user_id="alice",
    conversation_id="daily_chat",
)

follow_up = await agent.chat(
    user_message="Summarize what this conversation has decided.",
    user_id="bob",
    conversation_id="daily_chat",
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
- `POST /clear_conversation`
- `GET /memory`

### `POST /chat`

Request body:

```json
{
  "user_id": "alice",
  "conversation_id": "daily_chat",
  "user_message": "Hello",
  "stream": false,
  "enable_memory": true
}
```

### `POST /clear_conversation`

Request body:

```json
{
  "conversation_id": "daily_chat"
}
```

### `GET /memory`

Query parameters:

- `query`
- `limit`

This endpoint returns the agent-global memory view.

## Message Storage

### MessageStorageBase

Base interface for transcript storage.

```python
async def add_messages(conversation_id: str, messages, **kwargs) -> None
async def get_messages(conversation_id: str, count: int = 20) -> list[Message]
async def clear_conversation(conversation_id: str) -> None
async def pop_message(conversation_id: str) -> Message | None
async def get_message_count(conversation_id: str) -> int
async def has_messages(conversation_id: str) -> bool
def get_conversation_info(conversation_id: str) -> dict[str, str]
```

### MessageStorageLocal

SQLite-backed local message storage.

```python
from xagent.components import MessageStorageLocal

storage = MessageStorageLocal()
agent = Agent(message_storage=storage)
```

### MessageStorageCloud

Redis-backed message storage.

```python
from xagent.components import MessageStorageCloud

storage = MessageStorageCloud()
agent = Agent(message_storage=storage)
```

Requires `REDIS_URL`.

## Memory Storage

### MemoryStorageBase

```python
async def add(memory_key: str, conversation_id: str, messages: list[dict]) -> None
async def store(memory_key: str, content: str) -> str | None
async def retrieve(memory_key: str, query: str, limit: int = 5) -> list | None
async def clear(memory_key: str) -> None
async def delete(memory_ids: list[str]) -> None
```

### Memory Behavior

- Runtime memory is agent-global
- All conversations and all speakers contribute to the same long-term memory pool for that agent
- Retrieved memory can therefore carry context across conversations and users

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
