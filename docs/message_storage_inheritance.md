# MessageStorageBase Inheritance Guide

`MessageStorageBase` stores conversation transcripts keyed by `conversation_id`.

This matches the unified conversation model:

- one transcript per `conversation_id`
- `user_id` lives on each message as speaker metadata
- the storage layer does not partition by user

## Interface

```python
class MessageStorageBase(ABC):
    async def add_messages(
        self,
        conversation_id: str,
        messages: Message | list[Message],
        **kwargs,
    ) -> None: ...

    async def get_messages(
        self,
        conversation_id: str,
        count: int = 20,
    ) -> list[Message]: ...

    async def clear_conversation(self, conversation_id: str) -> None: ...

    async def pop_message(self, conversation_id: str) -> Message | None: ...

    async def get_message_count(self, conversation_id: str) -> int: ...

    async def has_messages(self, conversation_id: str) -> bool: ...

    def get_conversation_info(self, conversation_id: str) -> dict[str, str]: ...
```

## Message Shape

Stored messages should preserve:

- `role`
- `content`
- `sender_id`
- tool metadata when applicable

`sender_id` is what allows the runtime to distinguish speakers in a shared transcript.

## Built-in Implementations

### MessageStorageLocal

- SQLite-backed
- durable local transcript storage
- good default for development and local deployment

### MessageStorageCloud

- Redis-backed
- good for distributed deployments
- requires `REDIS_URL`

## Example

```python
from xagent.components import MessageStorageLocal
from xagent.core import Agent

storage = MessageStorageLocal()
agent = Agent(message_storage=storage)

reply = await agent.chat(
    user_message="Hello",
    user_id="alice",
    conversation_id="daily_chat",
)
```

## Writing a Custom Backend

```python
from xagent.components.message.base_messages import MessageStorageBase


class MessageStorageCustom(MessageStorageBase):
    async def add_messages(self, conversation_id: str, messages, **kwargs):
        ...

    async def get_messages(self, conversation_id: str, count: int = 20):
        ...

    async def clear_conversation(self, conversation_id: str):
        ...

    async def pop_message(self, conversation_id: str):
        ...

    def get_conversation_info(self, conversation_id: str):
        return {
            "conversation_id": conversation_id,
            "backend": "custom",
        }
```

## Design Guidance

- Keep storage keyed by `conversation_id`
- Preserve chronological ordering
- Make batch writes atomic where possible
- Do not reinterpret speaker metadata inside storage
- Keep the storage layer focused on transcript persistence only
