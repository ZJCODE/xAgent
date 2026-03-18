# MessageStorageBase Inheritance Guide

`MessageStorageBase` stores a single continuous message stream for one agent.

This matches the runtime model:

- `user_id` lives on each message as speaker metadata
- the storage layer does not partition by user

## Interface

```python
class MessageStorageBase(ABC):
    async def add_messages(
        self,
        messages: Message | list[Message],
        **kwargs,
    ) -> None: ...

    async def get_messages(
        self,
        count: int = 100,
    ) -> list[Message]: ...

    async def clear_messages(self) -> None: ...

    async def pop_message(self) -> Message | None: ...

    async def get_message_count(self) -> int: ...

    async def has_messages(self) -> bool: ...

    def get_stream_info(self) -> dict[str, str]: ...
```

## Message Shape

Stored messages should preserve:

- `role`
- `content`
- `sender_id`
- tool metadata when applicable

`sender_id` is what allows the runtime to distinguish speakers in the shared stream.

## Built-in Implementations

### MessageStorageLocal

- SQLite-backed
- durable local transcript storage
- good default for development and local deployment

## Example

```python
from xagent.components import MessageStorageLocal
from xagent.core import Agent

storage = MessageStorageLocal()
agent = Agent(message_storage=storage)

reply = await agent.chat(
    user_message="Hello",
    user_id="alice",
)
```

## Writing a Custom Backend

```python
from xagent.components.message.base_messages import MessageStorageBase


class MessageStorageCustom(MessageStorageBase):
    async def add_messages(self, messages, **kwargs):
        ...

    async def get_messages(self, count: int = 100):
        ...

    async def clear_messages(self):
        ...

    async def pop_message(self):
        ...

    def get_stream_info(self):
        return {
            "stream": "custom",
            "backend": "custom",
        }
```

## Design Guidance

- Keep storage append-only and chronologically ordered
- Preserve chronological ordering
- Make batch writes atomic where possible
- Do not reinterpret speaker metadata inside storage
- Keep the storage layer focused on transcript persistence only
