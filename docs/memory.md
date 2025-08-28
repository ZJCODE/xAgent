# Memory System Documentation

The xAgent Memory System provides agents with long-term memory capabilities, allowing them to store, process, and retrieve contextually relevant information across multiple conversations. This enables more personalized and contextually aware interactions.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Storage Backends](#storage-backends)
- [Configuration](#configuration)
- [Usage Examples](#usage-examples)
- [Memory Types](#memory-types)
- [API Reference](#api-reference)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

## Overview

The memory system automatically processes conversations and extracts meaningful information that can be recalled in future interactions. It uses LLM-powered extraction and semantic search to ensure relevant memories are surfaced when needed.

### Key Features

- **Automatic Memory Extraction**: Intelligently identifies and stores important information from conversations
- **Semantic Search**: Uses embeddings to find contextually relevant memories
- **Multiple Storage Backends**: Supports both local (ChromaDB) and cloud (Upstash Vector) storage
- **User Isolation**: Memories are stored per user for privacy and personalization
- **Configurable Thresholds**: Control when and how memories are created
- **LLM Processing**: Uses AI to understand and categorize memories

### Memory Types

The system automatically categorizes memories into different types:

1. **Profile**: Personal information about users (name, job, preferences)
2. **Episodic**: Specific events and experiences shared in conversations
5. **Meta**: High-level insights and patterns extracted from multiple conversations

## Quick Start

### Basic Usage

```python
import asyncio
from xagent.core import Agent

async def basic_memory_example():
    agent = Agent(name="memory_agent")
    
    # First conversation - establishing context
    response1 = await agent.chat(
        user_message="Hi, I'm Sarah. I'm a data scientist at Netflix and I love hiking in the mountains.",
        user_id="sarah_123",
        session_id="intro",
        enable_memory=True
    )
    
    # Later conversation - agent recalls previous context
    response2 = await agent.chat(
        user_message="Can you recommend some hiking spots?",
        user_id="sarah_123",
        session_id="hiking_request", 
        enable_memory=True
    )
    # Agent will remember Sarah's love for hiking and provide personalized recommendations

asyncio.run(basic_memory_example())
```

### Agent Configuration with Memory

```python
from xagent.core import Agent
from xagent.components.memory import MemoryStorageLocal

# Configure memory storage
memory_storage = MemoryStorageLocal(
    collection_name="my_agent_memories",
    memory_threshold=5,  # Store memories after 5 messages
    keep_recent=2        # Keep 2 recent messages after storage
)

# Create agent with custom memory
agent = Agent(
    name="personal_assistant",
    system_prompt="You are a personal assistant with excellent memory.",
    memory_storage=memory_storage
)
```

## Storage Backends

### Local Storage (ChromaDB)

Default storage option that runs locally without external dependencies.

```python
from xagent.components.memory import MemoryStorageLocal

local_memory = MemoryStorageLocal(
    path="/path/to/storage",           # Custom storage path
    collection_name="agent_memory",    # Collection name
    memory_threshold=10,               # Messages before storage
    keep_recent=2                      # Recent messages to keep
)
```

**Advantages:**
- No external dependencies
- Fast local access
- Privacy (data stays local)
- No usage costs

**Use Cases:**
- Development and testing
- Single-user applications
- Privacy-sensitive deployments

### Upstash Vector Storage

Cloud-based vector database for production deployments.

```python
from xagent.components.memory import MemoryStorageUpstash

upstash_memory = MemoryStorageUpstash(
    memory_threshold=10,
    keep_recent=2
)
```

**Required Environment Variables:**
```bash
UPSTASH_VECTOR_REST_URL=https://your-database.upstash.io
UPSTASH_VECTOR_REST_TOKEN=your_token_here
REDIS_URL=redis://username:password@host:port/database
```

**Advantages:**
- Scalable cloud storage
- Built for production
- High availability
- Automatic backups

**Use Cases:**
- Production deployments
- Multi-user applications
- Distributed systems

## Configuration

### Memory Thresholds

Control when memories are created and stored:

```python
memory = MemoryStorageLocal(
    memory_threshold=10,  # Store after 10 messages
    keep_recent=2         # Keep 2 recent messages
)
```

### Environment Variables

```bash
# OpenAI API (required for embeddings and LLM processing)
OPENAI_API_KEY=your_openai_api_key

# Upstash Vector (for cloud storage)
UPSTASH_VECTOR_REST_URL=your_upstash_vector_url
UPSTASH_VECTOR_REST_TOKEN=your_upstash_vector_token

# Redis (for Upstash temporary storage)
REDIS_URL=redis://username:password@host:port/database

# Optional: Custom ChromaDB path
CHROMADB_PATH=/custom/path/to/chromadb
```

### Agent Integration

```python
agent = Agent(
    name="assistant",
    memory_storage=memory_storage,
    # ... other configuration
)

# Enable memory in conversations
response = await agent.chat(
    user_message="Your message",
    user_id="unique_user_id",
    session_id="session_id",
    enable_memory=True  # Must be True to use memory
)
```

## Usage Examples

### Personal Assistant

```python
import asyncio
from xagent.core import Agent

async def personal_assistant_example():
    agent = Agent(
        name="personal_assistant",
        system_prompt="You are a helpful personal assistant who remembers important details about users."
    )
    
    # Store personal preferences
    await agent.chat(
        user_message="I prefer vegetarian restaurants and I'm allergic to nuts. My favorite cuisine is Italian.",
        user_id="user_123",
        enable_memory=True
    )
    
    # Later, get restaurant recommendations
    response = await agent.chat(
        user_message="Can you recommend a restaurant for dinner tonight?",
        user_id="user_123",
        enable_memory=True
    )
    # Agent will remember dietary preferences and suggest vegetarian Italian restaurants

asyncio.run(personal_assistant_example())
```

### Learning Companion

```python
async def learning_companion_example():
    agent = Agent(
        name="tutor",
        system_prompt="You are a patient tutor who tracks student progress and adapts to their learning style."
    )
    
    # Track learning progress
    await agent.chat(
        user_message="I'm struggling with calculus derivatives. Visual examples help me learn better.",
        user_id="student_456",
        enable_memory=True
    )
    
    # Later session adapts to known learning style
    response = await agent.chat(
        user_message="Can you help me with integration?",
        user_id="student_456", 
        enable_memory=True
    )
    # Agent remembers visual learning preference and provides diagrams/examples

asyncio.run(learning_companion_example())
```

### Project Management Assistant

```python
async def project_management_example():
    agent = Agent(
        name="project_manager",
        system_prompt="You are a project management assistant who tracks ongoing projects and deadlines."
    )
    
    # Store project information
    await agent.chat(
        user_message="I'm working on a mobile app project due next Friday. The team includes 3 developers and 1 designer.",
        user_id="pm_789",
        enable_memory=True
    )
    
    # Check project status later
    response = await agent.chat(
        user_message="What's the status of my current projects?",
        user_id="pm_789",
        enable_memory=True
    )
    # Agent recalls project details and can provide status updates

asyncio.run(project_management_example())
```

### Customer Service Agent

```python
async def customer_service_example():
    agent = Agent(
        name="support_agent",
        system_prompt="You are a customer service agent who remembers customer history and preferences."
    )
    
    # Store customer issue history
    await agent.chat(
        user_message="I had an issue with my order #12345 last week. The replacement arrived damaged too.",
        user_id="customer_001",
        enable_memory=True
    )
    
    # Handle follow-up efficiently
    response = await agent.chat(
        user_message="I need help with another order issue.",
        user_id="customer_001",
        enable_memory=True
    )
    # Agent remembers previous issues and can provide proactive support

asyncio.run(customer_service_example())
```

## Memory Types

### Profile Memory
Stores personal information about users:

```python
# Example profile information that gets stored:
# - Name, age, occupation
# - Location and contact preferences  
# - Personal interests and hobbies
# - Family and relationship details
# - Preferences and dislikes
```

### Episodic Memory
Records specific events and experiences:

```python
# Example episodic memories:
# - "User went to Paris last summer"
# - "Had a successful presentation at work"
# - "Attended a wedding last weekend"
# - "Completed a marathon in October"
```


### Meta Memory
High-level insights and patterns:

```python
# Example meta memory:
# - "User prefers visual learning materials"
# - "Works best with morning meetings"
# - "Tends to ask detailed technical questions"
# - "Responds well to encouraging feedback"
```

## API Reference

### MemoryStorageBase

Abstract base class for memory storage implementations.

```python
class MemoryStorageBase(ABC):
    async def add(self, user_id: str, messages: List[Dict[str, Any]]) -> None
    async def store(self, user_id: str, content: str) -> Optional[str]
    async def retrieve(self, user_id: str, query: str, limit: int = 5) -> Optional[List[str]]
    async def extract_meta(self, user_id: str, days: int = 1) -> Optional[List[str]]
    async def clear(self, user_id: str) -> None
    async def delete(self, memory_ids: List[str]) -> None
```

### MemoryStorageLocal

Local ChromaDB implementation.

```python
class MemoryStorageLocal(MemoryStorageBase):
    def __init__(
        self,
        path: str = None,                    # Storage directory path
        collection_name: str = "xagent_memory",  # Collection name
        memory_threshold: int = 10,          # Message threshold for storage
        keep_recent: int = 2                 # Recent messages to keep
    )
```

### MemoryStorageUpstash

Upstash Vector implementation.

```python
class MemoryStorageUpstash(MemoryStorageBase):
    def __init__(
        self,
        memory_threshold: int = 10,          # Message threshold for storage
        keep_recent: int = 2                 # Recent messages to keep
    )
```

### Agent Memory Integration

```python
# Agent initialization with memory
agent = Agent(
    name="agent_name",
    memory_storage=memory_storage,  # MemoryStorageBase instance
    # ... other parameters
)

# Chat with memory enabled
response = await agent.chat(
    user_message="message",
    user_id="user_id",           # Required for memory isolation
    session_id="session_id",     # Session identifier
    enable_memory=True           # Enable memory for this conversation
)
```

## Best Practices

### User ID Management

Always use consistent, unique user IDs:

```python
# Good: Consistent user identification
user_id = f"user_{hash(email_address)}"
await agent.chat(user_message=msg, user_id=user_id, enable_memory=True)

# Bad: Inconsistent user IDs break memory continuity
await agent.chat(user_message=msg, user_id="random_id", enable_memory=True)
```

### Memory Threshold Tuning

Adjust thresholds based on your use case:

```python
# For chatbots with frequent short interactions
memory = MemoryStorageLocal(memory_threshold=5, keep_recent=1)

# For detailed consultations or tutoring
memory = MemoryStorageLocal(memory_threshold=15, keep_recent=3)

# For long-form conversations
memory = MemoryStorageLocal(memory_threshold=20, keep_recent=5)
```

### Privacy Considerations

Implement proper data handling:

```python
# Clear memories when user requests deletion
await memory.clear(user_id="user_to_delete")

# Delete specific memories
memory_ids = ["memory_id_1", "memory_id_2"]
await memory.delete(memory_ids)

# Use hashed user IDs for additional privacy
import hashlib
user_id = hashlib.sha256(real_user_id.encode()).hexdigest()
```

### Production Deployment

For production environments:

```python
# Use Upstash Vector for scalability
memory = MemoryStorageUpstash(
    memory_threshold=10,
    keep_recent=2
)

# Implement error handling
try:
    response = await agent.chat(
        user_message=message,
        user_id=user_id,
        enable_memory=True
    )
except Exception as e:
    logger.error(f"Memory error: {e}")
    # Fallback to conversation without memory
    response = await agent.chat(
        user_message=message,
        user_id=user_id,
        enable_memory=False
    )
```

### Performance Optimization

Optimize memory usage:

```python
# Limit retrieval results for performance
retrieved_memories = await memory.retrieve(
    user_id=user_id,
    query=query,
    limit=3  # Limit to most relevant memories
)

# Use appropriate memory thresholds
# Higher thresholds = fewer but richer memories
# Lower thresholds = more frequent but potentially noisier memories
```

## Troubleshooting

### Common Issues

#### Memory Not Being Stored

**Symptoms**: Agent doesn't remember previous conversations

**Solutions**:
1. Ensure `enable_memory=True` in chat calls
2. Check if memory threshold is reached
3. Verify OpenAI API key is set
4. Check logs for memory storage errors

```python
# Debug memory storage
import logging
logging.getLogger('MemoryStorageLocal').setLevel(logging.DEBUG)

# Force memory storage for testing
await memory.store(user_id="test_user", content="Test memory content")
```

#### ChromaDB Connection Issues

**Symptoms**: ChromaDB initialization errors

**Solutions**:
1. Check directory permissions for storage path
2. Ensure sufficient disk space
3. Verify ChromaDB installation

```bash
# Reinstall ChromaDB if needed
pip install --upgrade chromadb
```

#### Upstash Connection Issues

**Symptoms**: Upstash Vector API errors

**Solutions**:
1. Verify environment variables are set correctly
2. Check Upstash dashboard for API limits
3. Test connection independently

```python
# Test Upstash connection
from upstash_vector import Index
index = Index.from_env()
result = index.info()
print(result)
```

#### Memory Retrieval Problems

**Symptoms**: Irrelevant memories being retrieved

**Solutions**:
1. Improve query specificity
2. Adjust retrieval limits
3. Review memory content quality

```python
# Debug memory retrieval
memories = await memory.retrieve(
    user_id=user_id,
    query=query,
    limit=10  # Retrieve more to analyze relevance
)
for mem in memories:
    print(f"Memory: {mem['content'][:100]}...")
    print(f"Score: {mem.get('score', 'N/A')}")
```

### Debugging Tools

Enable detailed logging:

```python
import logging

# Enable memory system logging
logging.getLogger('MemoryStorageLocal').setLevel(logging.DEBUG)
logging.getLogger('MemoryStorageUpstash').setLevel(logging.DEBUG)
logging.getLogger('MemoryLLMService').setLevel(logging.DEBUG)

# Custom logger for your application
logger = logging.getLogger(__name__)
logger.info("Memory debugging enabled")
```

Test memory operations directly:

```python
async def test_memory_operations():
    memory = MemoryStorageLocal()
    user_id = "test_user"
    
    # Test storage
    memory_id = await memory.store(user_id, "Test memory content")
    print(f"Stored memory ID: {memory_id}")
    
    # Test retrieval
    results = await memory.retrieve(user_id, "test content", limit=5)
    print(f"Retrieved {len(results)} memories")
    
    # Test clearing
    await memory.clear(user_id)
    print("Cleared all memories for user")
```

### Performance Monitoring

Monitor memory system performance:

```python
import time

async def timed_memory_operation():
    start_time = time.time()
    
    # Your memory operation
    response = await agent.chat(
        user_message=message,
        user_id=user_id,
        enable_memory=True
    )
    
    end_time = time.time()
    print(f"Memory-enabled chat took {end_time - start_time:.2f} seconds")
```

## Support

For additional support:

- Check the [examples directory](../examples/demo/) for working code samples
- Review the [API reference documentation](api_reference.md)
- Report issues on the project GitHub repository
- Join the community discussions for best practices and tips

---

*This documentation covers the core functionality of the xAgent Memory System. For the latest updates and advanced features, please refer to the source code and example implementations.*
