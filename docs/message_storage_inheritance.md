# MessageStorageBase Inheritance Guide

## Overview

`MessageStorageBase` is an abstract base class that defines a unified interface for message storage backends in the xAgent framework. It ensures consistency between different storage implementations and supports multi-user, multi-session conversation history management.

## Class Hierarchy

```
MessageStorageBase (Abstract Base Class)
├── MessageStorageLocal (In-Memory Storage)
└── MessageStorageRedis (Redis Storage)
```

## Abstract Base Class: MessageStorageBase

### Location
- File Path: `xagent/components/message/base_messages.py`
- Import Path: `from xagent.components import MessageStorageBase`

### Design Goals

- **Interface Consistency**: Provides a unified API interface for all message storage backends
- **Multi-User Support**: Implements user and session isolation through `user_id` and `session_id`
- **Extensibility**: Facilitates adding new storage backend implementations
- **Compatibility**: Ensures seamless switching between different storage backends

### Core Abstract Methods

All inheriting classes must implement the following abstract methods:

#### 1. `add_messages()`
```python
async def add_messages(
    self,
    user_id: str,
    session_id: str,
    messages: Union[Message, List[Message]],
    **kwargs
) -> None
```
- **Function**: Add messages to session history
- **Parameters**: Supports single Message object or list of Message objects
- **Extension**: Supports backend-specific parameters through `**kwargs`

#### 2. `get_messages()`
```python
async def get_messages(
    self, 
    user_id: str, 
    session_id: str, 
    count: int = 20
) -> List[Message]
```
- **Function**: Get the last N messages from session history
- **Returns**: Chronologically ordered list of messages
- **Default**: Returns the latest 20 messages by default

#### 3. `clear_history()`
```python
async def clear_history(self, user_id: str, session_id: str) -> None
```
- **Function**: Clear the session history
- **Use Cases**: Reset conversations or delete sensitive information

#### 4. `pop_message()`
```python
async def pop_message(self, user_id: str, session_id: str) -> Optional[Message]
```
- **Function**: Remove and return the last message from session
- **Special Handling**: Usually skips tool messages, only returns user/assistant messages
- **Returns**: The last message, or None if empty

### Optional Methods (With Default Implementations)

#### 1. `get_message_count()`
```python
async def get_message_count(self, user_id: str, session_id: str) -> int
```
- **Default Implementation**: Gets all messages via `get_messages()` and counts them
- **Optimization Suggestion**: Subclasses can override for more efficient implementations

#### 2. `has_messages()`
```python
async def has_messages(self, user_id: str, session_id: str) -> bool
```
- **Default Implementation**: Checks if `get_message_count()` is greater than 0
- **Purpose**: Quickly check if session contains messages

#### 3. `get_session_info()`
```python
def get_session_info(self, user_id: str, session_id: str) -> Dict[str, str]
```
- **Default Implementation**: Returns basic session information
- **Extension Suggestion**: Subclasses should override to provide backend-specific information

## Concrete Implementation Classes

### 1. MessageStorageLocal (In-Memory Storage)

#### Features
- **Storage Method**: Dictionary structure in memory
- **Lifecycle**: Data is lost when application restarts
- **Use Cases**: Development testing, temporary sessions, scenarios not requiring persistence

#### Core Characteristics
```python
class MessageStorageLocal(MessageStorageBase):
    def __init__(self):
        self._messages: Dict[Tuple[str, str], List[Message]] = {}
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
```

- **Storage Format**: `{(user_id, session_id): [Message, ...]}`
- **History Management**: Automatically trims to maximum history length (default 100 messages)
- **Session Isolation**: Implements user and session isolation through tuple keys

#### Configuration Parameters
```python
class MessageStorageLocalConfig:
    DEFAULT_MESSAGE_COUNT = 20      # Default number of messages to return
    MAX_LOCAL_HISTORY = 100         # Maximum number of historical messages
```

#### Additional Methods
- `get_all_sessions()`: Get list of all sessions
- `clear_all_sessions()`: Clear all session data

### 2. MessageStorageRedis (Redis Storage)

#### Features
- **Storage Method**: Redis list structure
- **Persistence**: Supports data persistence and recovery
- **Use Cases**: Production environments, scenarios requiring persistence, high-concurrency scenarios

#### Core Characteristics
```python
class MessageStorageRedis(MessageStorageBase):
    def __init__(self, redis_url: Optional[str] = None, *, sanitize_keys: bool = False):
        self.redis_url = self._get_redis_url(redis_url)
        self.r: Optional[redis.Redis] = None
        self.sanitize_keys = sanitize_keys
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
```

- **Storage Format**: Redis key-value pairs, keys are `"chat:<user_id>:<session_id>"`
- **Message Serialization**: Stores Message objects in JSON format
- **TTL Support**: Automatic expiration mechanism (default 30 days)
- **Atomic Operations**: Uses Redis pipelines to ensure data consistency

#### Configuration Parameters
```python
class MessageStorageRedisConfig:
    MSG_PREFIX = "chat"             # Redis key prefix
    DEFAULT_TTL = 2592000           # Default expiration time (30 days)
    DEFAULT_MESSAGE_COUNT = 20      # Default number of messages to return
    DEFAULT_MAX_HISTORY = 200       # Default maximum history count
    CLIENT_NAME = "xagent-message-storage"
```

#### Advanced Features
- **Connection Management**: Lazy loading connections, connection pool support
- **Health Checks**: `ping()` method to check connection status
- **History Trimming**: `trim_history()` method to manage history length
- **Key Sanitization**: Optional URL encoding support
- **Async Context**: Supports `async with` syntax

#### Additional Methods
- `ping()`: Check Redis connection status
- `close()`: Close Redis connection
- `trim_history()`: Trim historical messages
- `get_key_info()`: Get Redis key information

## Usage Examples

### In-Memory Storage
```python
from xagent.components import MessageStorageLocal, Agent

# Create in-memory storage
storage = MessageStorageLocal()

# Create agent using in-memory storage
agent = Agent(
    name="my_agent",
    message_storage=storage
)

# Use the agent
response = await agent.chat("Hello", user_id="user1", session_id="session1")
```

### Redis Storage
```python
from xagent.components import MessageStorageRedis, Agent
import os

# Set Redis connection
os.environ["REDIS_URL"] = "redis://localhost:6379/0"

# Create Redis storage
storage = MessageStorageRedis(sanitize_keys=True)

# Create agent using Redis storage
agent = Agent(
    name="my_agent", 
    message_storage=storage
)

# Use the agent
response = await agent.chat("Hello", user_id="user1", session_id="session1")

# Check connection status
is_connected = await storage.ping()

# Manually close connection
await storage.close()
```

### Storage Switching
```python
# Choose storage backend based on environment
def create_storage():
    if os.getenv("REDIS_URL"):
        return MessageStorageRedis()
    else:
        return MessageStorageLocal()

agent = Agent(message_storage=create_storage())
```

## Extending New Storage Backends

### Implementation Steps

1. **Inherit Base Class**
```python
from xagent.components.message.base_messages import MessageStorageBase

class MessageStorageCustom(MessageStorageBase):
    def __init__(self, connection_params):
        # Initialize custom storage connection
        pass
```

2. **Implement Abstract Methods**
```python
async def add_messages(self, user_id: str, session_id: str, messages, **kwargs):
    # Implement add messages logic
    pass

async def get_messages(self, user_id: str, session_id: str, count: int = 20):
    # Implement get messages logic
    pass

async def clear_history(self, user_id: str, session_id: str):
    # Implement clear history logic
    pass

async def pop_message(self, user_id: str, session_id: str):
    # Implement pop message logic
    pass
```

3. **Override Optional Methods** (Recommended)
```python
def get_session_info(self, user_id: str, session_id: str):
    # Return custom backend information
    return {
        "user_id": user_id,
        "session_id": session_id,
        "backend": "custom",
        "custom_info": "additional_data"
    }
```

4. **Register and Use**
```python
# Register in components
from xagent.components import MessageStorageLocal, MessageStorageRedis
from .custom_storage import MessageStorageCustom

# Use custom storage
agent = Agent(message_storage=MessageStorageCustom(connection_params))
```

## Best Practices

### 1. Error Handling
- All methods should properly handle exceptions
- Use appropriate exception types (ValueError, ConnectionError, etc.)
- Provide meaningful error messages

### 2. Logging
- Use class-level logger
- Log critical operations and error information
- Follow consistent logging format

### 3. Performance Optimization
- Implement efficient `get_message_count()` and `has_messages()`
- Consider batch operations and connection pooling
- Set reasonable historical message limits

### 4. Data Consistency
- Use transactions or atomic operations
- Handle concurrent access scenarios
- Ensure data integrity

### 5. Configuration Management
- Provide configuration classes or constants
- Support environment variable configuration
- Provide reasonable default values

## Important Notes

1. **Thread Safety**: Ensure implementation is async-safe
2. **Resource Management**: Properly handle connections and resource cleanup
3. **Backward Compatibility**: Maintain interface stability
4. **Test Coverage**: Provide unit tests for all methods
5. **Complete Documentation**: Provide clear class and method documentation

By following the design patterns of `MessageStorageBase`, you can easily create message storage backends that meet specific needs while maintaining full compatibility with the xAgent framework.
