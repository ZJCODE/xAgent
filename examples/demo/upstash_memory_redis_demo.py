"""
Upstash Memory with Redis User Message Storage Example

This example demonstrates how the updated UpstashMemory class now uses Redis
to store temporary user messages instead of local memory, making it completely
stateless and suitable for distributed environments.
"""

import asyncio
import os
from xagent.components.memory.upstash_memory import MemoryStorageUpstash

async def test_upstash_memory_redis_storage():
    """Test the Upstash Memory with Redis storage for user messages."""
    
    # Initialize Upstash Memory with Redis backend for temporary messages
    memory = MemoryStorageUpstash(
        memory_threshold=3,  # Low threshold for demo
        keep_recent=1        # Keep only 1 message after storage
    )
    
    user_id = "demo_user"
    
    print("=== Testing Upstash Memory with Redis User Message Storage ===")
    
    # Test 1: Add some messages (should be stored in Redis)
    print("\n1. Adding messages to temporary Redis storage...")
    messages_batch1 = [
        {"role": "user", "content": "Hello, I'm learning about machine learning"},
        {"role": "assistant", "content": "Great! Machine learning is fascinating. What specific area interests you?"},
        {"role": "user", "content": "I'm particularly interested in deep learning and neural networks"}
    ]
    
    await memory.add(user_id, messages_batch1)
    print(f"Added {len(messages_batch1)} messages for user {user_id}")
    
    # Add one more message to trigger storage (threshold=3)
    print("\n2. Adding trigger message...")
    trigger_message = [{"role": "assistant", "content": "Deep learning is a subset of machine learning that uses neural networks with multiple layers."}]
    
    await memory.add(user_id, trigger_message)
    print("Added trigger message - should have triggered long-term storage")
    
    # Test 2: Retrieve memories from long-term storage
    print("\n3. Retrieving memories from long-term storage...")
    retrieved_memories = await memory.retrieve(
        user_id=user_id,
        query="machine learning and neural networks",
        limit=3
    )
    
    print(f"Retrieved {len(retrieved_memories)} memories:")
    for i, mem in enumerate(retrieved_memories, 1):
        print(f"  Memory {i}: {mem['content'][:100]}...")
        print(f"    Type: {mem['metadata'].get('memory_type', 'unknown')}")
    
    # Test 3: Add more messages with keyword trigger
    print("\n4. Testing keyword trigger...")
    keyword_messages = [
        {"role": "user", "content": "Can you please help me remember this important information about my project?"}
    ]
    
    await memory.add(user_id, keyword_messages)
    print("Added message with keyword trigger")
    
    # Test 4: Retrieve updated memories
    print("\n5. Retrieving updated memories...")
    updated_memories = await memory.retrieve(
        user_id=user_id,
        query="important information project",
        limit=5
    )
    
    print(f"Retrieved {len(updated_memories)} updated memories:")
    for i, mem in enumerate(updated_memories, 1):
        print(f"  Memory {i}: {mem['content'][:100]}...")
    
    # Test 5: Test meta memory extraction
    print("\n6. Testing meta memory extraction...")
    meta_memory_ids = await memory.extract_meta(user_id, days=1)
    print(f"Extracted {len(meta_memory_ids)} meta memory pieces")
    
    # Test 6: Clear all memories
    print("\n7. Cleaning up - clearing all memories...")
    await memory.clear(user_id)
    print("Cleared all memories for user")
    
    # Close Redis connection
    await memory.close()
    print("Closed Redis connections")
    
    print("\n=== Test completed successfully! ===")

async def demo_distributed_usage():
    """Demonstrate how the Redis-backed storage works across multiple instances."""
    
    print("\n=== Demonstrating Distributed Usage ===")
    
    # Create two separate memory instances (simulating different servers/processes)
    memory1 = MemoryStorageUpstash(memory_threshold=2, keep_recent=1)
    memory2 = MemoryStorageUpstash(memory_threshold=2, keep_recent=1)
    
    user_id = "distributed_user"
    
    # Instance 1 adds some messages
    print("\n1. Instance 1 adds messages...")
    messages1 = [
        {"role": "user", "content": "I'm working on a Python project"},
        {"role": "assistant", "content": "That's great! What kind of Python project?"}
    ]
    await memory1.add(user_id, messages1)
    print("Instance 1 added messages to Redis")
    
    # Instance 2 adds more messages (should see the previous ones from Redis)
    print("\n2. Instance 2 adds trigger message...")
    trigger_msg = [{"role": "user", "content": "It's a web application using FastAPI"}]
    await memory2.add(user_id, trigger_msg)
    print("Instance 2 added trigger message - should trigger storage of all messages")
    
    # Both instances can retrieve the stored memories
    print("\n3. Both instances retrieving memories...")
    
    memories1 = await memory1.retrieve(user_id, "Python FastAPI web application", limit=3)
    memories2 = await memory2.retrieve(user_id, "Python project development", limit=3)
    
    print(f"Instance 1 retrieved: {len(memories1)} memories")
    print(f"Instance 2 retrieved: {len(memories2)} memories")
    
    # Clean up
    await memory1.clear(user_id)
    await memory1.close()
    await memory2.close()
    
    print("Distributed demo completed!")

async def main():
    """Main function to run all tests."""
    
    # Check if required environment variables are set
    if not os.getenv("REDIS_URL"):
        print("Warning: REDIS_URL environment variable not set.")
        print("Please set REDIS_URL to test Redis functionality.")
        print("Example: export REDIS_URL='redis://localhost:6379/0'")
        return
    
    if not os.getenv("UPSTASH_VECTOR_REST_URL") or not os.getenv("UPSTASH_VECTOR_REST_TOKEN"):
        print("Warning: Upstash Vector environment variables not set.")
        print("Please set UPSTASH_VECTOR_REST_URL and UPSTASH_VECTOR_REST_TOKEN")
        return
    
    try:
        await test_upstash_memory_redis_storage()
        await demo_distributed_usage()
        
    except Exception as e:
        print(f"Error during testing: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
