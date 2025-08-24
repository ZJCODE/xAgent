"""
Local Memory Demo

This example demonstrates how to use the LocalMemory component to store and retrieve
memories with LLM-powered extraction and semantic search capabilities.
"""

import asyncio
from xagent.components.memory.local_memory import LocalMemory

async def main():
    # Initialize local memory
    print("🧠 Initializing Local Memory...")
    memory = LocalMemory(collection_name="demo_memory")
    
    # User information
    user_id = "user123"
    
    print("\n" + "="*60)
    print("📝 STORING MEMORIES FROM CONVERSATIONS")
    print("="*60)
    
    # Example 1: Store conversation with profile information
    conversation1 = """
    User: Hi, I'm John Smith. I live in San Francisco and work as a software engineer at Google. 
    I prefer vegetarian food and love Italian cuisine. My birthday is on July 15th, 1990.
    I usually wake up at 7 AM and go for a morning run in Golden Gate Park.
    
    Assistant: Nice to meet you, John! It's great to know about your preferences. 
    San Francisco is a wonderful city for vegetarian Italian food, and Golden Gate Park 
    is perfect for morning runs. I'll remember your birthday is coming up in July!
    """
    
    print(f"\n1. Storing conversation with profile information:")
    print(f"Conversation:\n{conversation1.strip()}")
    memory_id1 = await memory.store(user_id, conversation1)
    print(f"✅ Stored with ID: {memory_id1}")
    
    # Example 2: Store episodic conversation (restaurant booking)
    conversation2 = """
    User: Can you help me book a table at an Italian restaurant for my anniversary dinner this Saturday?
    
    Assistant: I'd be happy to help! Since you love Italian cuisine, let me suggest some great options. 
    What time would you prefer and how many people?
    
    User: It's for 2 people at 7 PM. Somewhere romantic would be nice.
    
    Assistant: Perfect! I found 'Bella Vista' which has great reviews and a romantic atmosphere. 
    They have vegetarian options too. I can help you make the reservation.
    
    User: That sounds perfect! Please book it for us.
    
    Assistant: Great! I've made the reservation at Bella Vista for 2 people at 7 PM this Saturday. 
    They'll hold the table under John Smith. Enjoy your anniversary dinner!
    """
    
    print(f"\n2. Storing episodic conversation (restaurant booking):")
    print(f"Conversation:\n{conversation2.strip()}")
    memory_id2 = await memory.store(user_id, conversation2)
    print(f"✅ Stored with ID: {memory_id2}")
    
    # Example 3: Store conversation with procedural knowledge
    conversation3 = """
    User: How do I make restaurant reservations in general? I'm not very experienced with this.
    
    Assistant: Here's a step-by-step guide for making restaurant reservations:
    1. Check the restaurant's availability on their website or call them
    2. Use their online booking system if available, or speak to the host
    3. Provide the date, time, and number of guests
    4. Give your contact information for confirmation
    5. Arrive 10-15 minutes early on the day of your reservation
    
    User: Thanks! That's really helpful. I'll keep this in mind for future bookings.
    
    Assistant: You're welcome! Feel free to ask if you need help with any specific reservations.
    """
    
    print(f"\n3. Storing conversation with procedural knowledge:")
    print(f"Conversation:\n{conversation3.strip()}")
    memory_id3 = await memory.store(user_id, conversation3)
    print(f"✅ Stored with ID: {memory_id3}")
    
    # Example 4: Store working memory conversation (current task)
    conversation4 = """
    User: I need help planning my birthday party for July 15th. It's coming up soon!
    
    Assistant: Happy early birthday! I'd love to help you plan. What kind of party are you thinking?
    
    User: Something casual but nice. I want to invite about 20 friends. My budget is around $500. 
    It should be in the San Francisco area since that's where I live.
    
    Assistant: Great! For a party of 20 with a $500 budget in San Francisco, we need to consider:
    - Finding a suitable venue (maybe a park pavilion or community center)
    - Food and drinks (keeping your vegetarian preferences in mind)
    - Decorations and entertainment
    Let me help you research some options.
    
    User: That sounds perfect. I especially want to make sure there are good vegetarian options.
    """
    
    print(f"\n4. Storing working memory conversation (current task):")
    print(f"Conversation:\n{conversation4.strip()}")
    memory_id4 = await memory.store(user_id, conversation4)
    print(f"✅ Stored with ID: {memory_id4}")
    
    # Example 5: Store conversation with semantic knowledge
    conversation5 = """
    User: What should I know about dining in San Francisco?
    
    Assistant: San Francisco has a fantastic dining scene! Here are some key things to know:
    - Italian restaurants typically require reservations on weekends, especially popular ones
    - The city is very vegetarian-friendly with lots of plant-based options
    - Golden Gate Park area has many great cafes if you want something after your morning runs
    - July weather is usually perfect for outdoor dining - warm and dry
    - Many restaurants offer both indoor and outdoor seating
    
    User: That's really useful to know! I'm excited to explore more restaurants here.
    
    Assistant: You'll love it! With your preferences for Italian and vegetarian food, you have lots of great options.
    """
    
    print(f"\n5. Storing conversation with semantic knowledge:")
    print(f"Conversation:\n{conversation5.strip()}")
    memory_id5 = await memory.store(user_id, conversation5)
    print(f"✅ Stored with ID: {memory_id5}")
    
    print("\n" + "="*60)
    print("🔍 RETRIEVING MEMORIES")
    print("="*60)
    
    # Test different queries to retrieve relevant memories
    queries = [
        "What food does the user like?",
        "How to make restaurant reservations?", 
        "What happened yesterday with the restaurant?",
        "User's birthday and personal information",
        "Current tasks and planning",
        "Information about San Francisco restaurants",
        "User's morning routine"
    ]
    
    for i, query in enumerate(queries, 1):
        print(f"\n{i}. Query: '{query}'")
        memories = await memory.retrieve(user_id, query, limit=3)
        
        if memories:
            print(f"   Found {len(memories)} relevant memories:")
            for j, mem in enumerate(memories, 1):
                print(f"   {j}. Type: {mem['metadata']['memory_type']}")
                print(f"      Content: {mem['content'][:100]}...")
                print(f"      Created: {mem['metadata']['created_at'][:19]}")
        else:
            print("   No relevant memories found.")
    
    print("\n" + "="*60)
    print("🎯 TARGETED QUERIES")
    print("="*60)
    
    # More specific queries to test precision
    specific_queries = [
        "vegetarian food preferences",
        "birthday date",
        "morning running routine",
        "Google software engineer",
        "anniversary dinner reservation"
    ]
    
    for i, query in enumerate(specific_queries, 1):
        print(f"\n{i}. Specific query: '{query}'")
        memories = await memory.retrieve(user_id, query, limit=2)
        
        if memories:
            for j, mem in enumerate(memories, 1):
                print(f"   Result {j}: {mem['content'][:150]}...")
        else:
            print("   No matches found.")
    
    print("\n" + "="*60)
    print("✨ DEMO COMPLETED SUCCESSFULLY!")
    print("="*60)
    print("""
📋 Summary:
- Stored 5 different types of memories (profile, episodic, procedural, working, semantic)
- Demonstrated automatic memory type classification and tagging
- Showed semantic search capabilities across different memory types
- Tested retrieval precision with specific queries

🔧 Key Features Demonstrated:
✅ Automatic memory extraction from natural language
✅ Intelligent memory type classification
✅ Semantic similarity search with ChromaDB
✅ Metadata tracking (timestamps, tags, user association)
✅ Support for multiple memory types (WORKING, PROFILE, EPISODIC, SEMANTIC, PROCEDURAL)

🚀 Next Steps:
- Try storing more complex conversations
- Experiment with different query types
- Integrate with Agent for automatic memory storage during conversations
    """)

if __name__ == "__main__":
    asyncio.run(main())
