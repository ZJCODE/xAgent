#!/usr/bin/env python3
"""
Demo for MetaMemory extraction from today's memories.

This example shows how to:
1. Store various types of memories throughout the day
2. Extract meta-level insights from today's memories
3. Store the meta memory for future reference
"""

import asyncio
import logging
from xagent.components.memory.local_memory import LocalMemory

# Set up logging
logging.basicConfig(level=logging.INFO)

async def meta_memory_demo():
    """Demonstrate meta memory extraction from today's memories."""
    
    # Initialize memory store
    memory = LocalMemory("meta_memory_demo")
    
    user_id = "demo_user"
    
    print("=== Meta Memory Demo ===\n")
    
    # Simulate storing various memories throughout the day
    print("1. Storing various memories throughout the day...")
    
    # Morning activities
    await memory.store(user_id, "User started the day by checking emails and found an important message from their manager about a project deadline next week.")
    
    # Work-related memories
    await memory.store(user_id, "User attended a team meeting where they discussed the new product launch strategy. They seemed particularly interested in the marketing approach.")
    
    # Personal preferences
    await memory.store(user_id, "User mentioned they prefer working from home on Fridays and would like to make it a permanent arrangement.")
    
    # Problem-solving
    await memory.store(user_id, "User encountered a technical issue with the authentication system and spent 2 hours debugging it. They eventually found the solution by checking the API documentation.")
    
    # Personal interests
    await memory.store(user_id, "User talked about planning a weekend trip to the mountains for hiking. They're excited about trying a new trail they discovered online.")
    
    # Learning activities
    await memory.store(user_id, "User completed an online course module about machine learning algorithms and took detailed notes about neural networks.")
    
    print("✓ Stored 6 different memories covering work, personal preferences, and activities\n")
    
    # Extract meta memory from today's memories
    print("2. Extracting meta-level insights from today's memories...")
    
    meta_memory = await memory.extract_meta_memory_from_today(user_id)
    
    print("✓ Meta Memory Analysis:")
    print(f"Extracted {len(meta_memory.contents)} meta-level insights:")
    for i, piece in enumerate(meta_memory.contents, 1):
        print(f"  {i}. [{piece.type.value.upper()}] {piece.content}")
    print()
    
    # Store the meta memory for future reference
    print("3. Storing meta memory for future reference...")
    
    meta_memory_ids = await memory.extract_and_store_meta_memory(user_id)
    
    print(f"✓ Meta memory stored with {len(meta_memory_ids)} memory pieces\n")
    
    # Demonstrate retrieving memories with meta context
    print("4. Demonstrating how meta memory enhances retrieval...")
    
    query = "What was the user interested in today?"
    relevant_memories = await memory.retrieve(user_id, query, limit=5)
    
    print(f"Query: '{query}'")
    print("Retrieved memories:")
    for i, memory_piece in enumerate(relevant_memories, 1):
        memory_type = memory_piece["metadata"].get("memory_type", "unknown")
        content = memory_piece['content']
        if len(content) > 80:
            content = content[:80] + "..."
        print(f"  {i}. [{memory_type.upper()}] {content}")
    
    print("\n5. Demonstrating meta-level query...")
    
    meta_query = "What patterns emerged from today's activities?"
    meta_results = await memory.retrieve(user_id, meta_query, limit=3)
    
    print(f"Meta Query: '{meta_query}'")
    print("Meta-level insights:")
    for i, memory_piece in enumerate(meta_results, 1):
        memory_type = memory_piece["metadata"].get("memory_type", "unknown")
        content = memory_piece['content']
        print(f"  {i}. [{memory_type.upper()}] {content}")
    
    print("\n=== Demo Complete ===")
    print("Meta memory extraction helps create high-level insights from daily activities,")
    print("enabling better understanding of user patterns, interests, and behavioral trends.")
    print("The system now stores multiple focused meta-insights rather than a single summary,")
    print("allowing for more granular and useful retrieval of high-level patterns.")

if __name__ == "__main__":
    asyncio.run(meta_memory_demo())
