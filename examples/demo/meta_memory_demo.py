"""
Meta Memory Demo

This example demonstrates how to use the LocalMemory component's meta memory extraction 
feature. Meta memory provides high-level insights and patterns derived from recent 
memories, helping to understand user context and behavioral patterns.
"""

import asyncio
import time
from datetime import datetime, timedelta
from xagent.components.memory.local_memory import LocalMemory

async def main():
    # Initialize local memory with unique collection for demo
    print("🧠 Initializing Local Memory for Meta Memory Demo...")
    memory = LocalMemory(collection_name="meta_memory_demo")
    
    # User information
    user_id = "user_meta_demo"
    
    print("\n" + "="*70)
    print("📝 STORING DIVERSE MEMORIES TO BUILD USER CONTEXT")
    print("="*70)
    
    # Store a variety of memories over simulated time to create patterns
    memories_to_store = [
        {
            "content": """
            User: Hi, I'm Alice Chen. I live in Beijing and work as a product manager at ByteDance. 
            I love cooking Asian cuisine, especially Korean and Japanese dishes. My birthday is on March 20th, 1992.
            I usually work from 9 AM to 6 PM and enjoy hiking on weekends.
            
            Assistant: Nice to meet you, Alice! It's great to know about your background. 
            Beijing has amazing food scenes for Asian cuisine lovers. I'll remember your preferences!
            """,
            "description": "Profile conversation with personal information"
        },
        {
            "content": """
            User: Can you help me find a Korean BBQ restaurant for my team dinner this Friday?
            
            Assistant: I'd be happy to help! Since you love Korean cuisine, let me suggest some great options in Beijing. 
            How many people will be joining, and do you have any dietary restrictions to consider?
            
            User: It's for 8 people from my product team. We don't have specific dietary restrictions.
            
            Assistant: Perfect! I found 'Seoul Garden' which has great reviews and can accommodate your team. 
            They have private rooms for business dinners. Would you like me to help with the reservation?
            
            User: Yes, that sounds great! Please book it for Friday at 7 PM.
            
            Assistant: Excellent! I've made the reservation at Seoul Garden for 8 people at 7 PM this Friday. 
            They'll hold the private room under Alice Chen. Enjoy your team dinner!
            """,
            "description": "Team dinner planning and restaurant booking"
        },
        {
            "content": """
            User: What's the best way to organize a product launch event?
            
            Assistant: Here's a comprehensive guide for organizing a successful product launch:
            1. Define your target audience and key messaging
            2. Plan the event timeline (3-6 months ahead for major launches)
            3. Choose the right venue and format (in-person, virtual, or hybrid)
            4. Coordinate with marketing, PR, and sales teams
            5. Prepare demo materials and product presentations
            6. Plan media outreach and influencer engagement
            7. Set up registration and attendance tracking
            8. Prepare follow-up strategies for leads and attendees
            
            User: This is really helpful! I'm planning a major product launch next quarter.
            
            Assistant: Great! Feel free to ask for specific help with any of these steps as you plan your launch.
            """,
            "description": "Procedural knowledge about product launch planning"
        },
        {
            "content": """
            User: I'm working on a new feature for our mobile app - a personalized recipe recommendation system.
            
            Assistant: That sounds like an exciting project! Given your love for Asian cuisine, you probably have good insights into what users might want. 
            What's your current approach for the recommendation algorithm?
            
            User: We're thinking of using collaborative filtering combined with ingredient-based matching. 
            The challenge is balancing popular recipes with personalized preferences.
            
            Assistant: That's a solid approach! You might also consider:
            - Seasonal ingredient availability for recommendations
            - Dietary restriction filters
            - Cooking skill level matching
            - Time-based suggestions (quick weeknight vs. weekend cooking)
            Let me know if you need help with any specific aspects!
            
            User: Thanks! I'll definitely need help with the user testing strategy later.
            """,
            "description": "Current work project discussion"
        },
        {
            "content": """
            User: Can you recommend some good hiking spots around Beijing for this weekend?
            
            Assistant: Since you enjoy weekend hiking, here are some great options near Beijing:
            1. Mutianyu Great Wall - scenic and less crowded than Badaling
            2. Fragrant Hills (Xiangshan) - beautiful autumn colors and moderate difficulty
            3. Jingshan Park - easier hike with great city views
            4. Yanshan Mountain - more challenging for experienced hikers
            5. Temple of Heaven Park - easy walking with cultural significance
            
            User: Fragrant Hills sounds perfect! How long does the hike usually take?
            
            Assistant: Fragrant Hills typically takes 2-3 hours for the main trail to the summit. 
            The autumn foliage is spectacular this time of year. Remember to bring water and wear comfortable shoes!
            
            User: Perfect! I'll plan to go there on Saturday morning.
            """,
            "description": "Weekend hiking planning"
        },
        {
            "content": """
            User: I've been feeling stressed with work lately. Any suggestions for managing work-life balance?
            
            Assistant: I understand work stress can be challenging. Here are some strategies that might help:
            1. Set clear boundaries between work and personal time
            2. Take regular breaks during the workday
            3. Use your lunch break to step away from your desk
            4. Try the Pomodoro technique for focused work sessions
            5. Schedule regular physical activities (like your weekend hikes!)
            6. Practice mindfulness or meditation
            7. Delegate tasks when possible
            8. Communicate openly with your team about workload
            
            User: Those are great suggestions. I think I need to be better about taking actual lunch breaks.
            
            Assistant: That's a great starting point! Even a 30-minute break can help reset your energy. 
            Maybe you could try a short walk or grab lunch at a nice restaurant - you know Beijing has amazing options!
            """,
            "description": "Work-life balance and stress management discussion"
        }
    ]
    
    # Store memories with small delays to simulate time progression
    stored_memory_ids = []
    for i, memory_data in enumerate(memories_to_store, 1):
        print(f"\n{i}. Storing: {memory_data['description']}")
        print(f"   Content preview: {memory_data['content'][:100].strip()}...")
        
        memory_id = await memory.store(user_id, memory_data['content'])
        stored_memory_ids.append(memory_id)
        print(f"   ✅ Stored with ID: {memory_id}")
        
        # Small delay to simulate time progression
        await asyncio.sleep(0.1)
    
    print(f"\n📊 Total memories stored: {len(stored_memory_ids)}")
    
    print("\n" + "="*70)
    print("🔍 TESTING RETRIEVAL BEFORE META EXTRACTION")
    print("="*70)
    
    # Test some queries to see what memories we have
    test_queries = [
        "What does Alice like to do on weekends?",
        "Alice's work and professional background",
        "Recent planning activities"
    ]
    
    for i, query in enumerate(test_queries, 1):
        print(f"\n{i}. Query: '{query}'")
        memories = await memory.retrieve(user_id, query, limit=3)
        
        if memories:
            print(f"   Found {len(memories)} relevant memories:")
            for j, mem in enumerate(memories, 1):
                print(f"   {j}. Type: {mem['metadata']['memory_type']}")
                print(f"      Content: {mem['content'][:120]}...")
        else:
            print("   No relevant memories found.")
    
    print("\n" + "="*70)
    print("🎯 EXTRACTING META MEMORY (1 DAY)")
    print("="*70)
    
    print("\nExtracting meta memory from today's activities...")
    meta_memory_ids = await memory.extract_meta(user_id, days=1)
    
    if meta_memory_ids:
        print(f"✅ Successfully extracted and stored {len(meta_memory_ids)} meta memory pieces")
        print(f"Meta memory IDs: {meta_memory_ids}")
    else:
        print("❌ No meta memories were extracted")
    
    print("\n" + "="*70)
    print("🔍 TESTING RETRIEVAL AFTER META EXTRACTION")
    print("="*70)
    
    # Test queries that should benefit from meta insights
    meta_queries = [
        "What patterns do you see in Alice's behavior?",
        "What are Alice's main interests and activities?",
        "Alice's work style and preferences",
        "What themes emerge from Alice's recent activities?",
        "Alice's lifestyle and personal patterns"
    ]
    
    for i, query in enumerate(meta_queries, 1):
        print(f"\n{i}. Meta Query: '{query}'")
        memories = await memory.retrieve(user_id, query, limit=4)
        
        if memories:
            print(f"   Found {len(memories)} relevant memories:")
            for j, mem in enumerate(memories, 1):
                memory_type = mem['metadata']['memory_type']
                source = mem['metadata'].get('source', 'original')
                
                # Highlight meta memories
                if memory_type == 'meta':
                    print(f"   🌟 {j}. [META] {source}")
                    print(f"       Content: {mem['content']}")
                    print(f"       Created: {mem['metadata']['created_at'][:19]}")
                else:
                    print(f"   {j}. [{memory_type.upper()}] Original memory")
                    print(f"      Content: {mem['content'][:100]}...")
        else:
            print("   No relevant memories found.")
    
    print("\n" + "="*70)
    print("📈 TESTING MULTI-DAY META EXTRACTION")
    print("="*70)
    
    # Simulate some additional activities to test multi-day extraction
    print("\nSimulating additional day of activities...")
    
    additional_memories = [
        {
            "content": """
            User: The Korean BBQ team dinner was amazing! The team really enjoyed Seoul Garden.
            
            Assistant: I'm so glad the dinner was a success! Seoul Garden is always a great choice. 
            How did your team like the private room setup?
            
            User: Perfect for our discussions. We actually brainstormed some great ideas for the product launch.
            
            Assistant: That's wonderful! Team dinners can be so productive when the environment is right. 
            Did you come up with any specific strategies for your upcoming launch?
            """,
            "description": "Follow-up on team dinner and product launch brainstorming"
        },
        {
            "content": """
            User: I went hiking at Fragrant Hills yesterday. The autumn colors were incredible!
            
            Assistant: That sounds amazing! I'm glad you took the time for your weekend hike. 
            Did it help with the work stress you mentioned earlier?
            
            User: Definitely! The fresh air and exercise really cleared my head. 
            I feel much more energized for the upcoming week.
            
            Assistant: That's fantastic! Regular outdoor activities like hiking can be so beneficial for both physical and mental health. 
            It sounds like you've found a great way to maintain work-life balance.
            """,
            "description": "Weekend hiking experience and stress relief"
        }
    ]
    
    for i, memory_data in enumerate(additional_memories, 1):
        print(f"\n{i}. Storing additional: {memory_data['description']}")
        memory_id = await memory.store(user_id, memory_data['content'])
        print(f"   ✅ Stored with ID: {memory_id}")
        await asyncio.sleep(0.1)
    
    # Extract meta memory for multi-day period
    print(f"\nExtracting meta memory from last 2 days of activities...")
    multi_day_meta_ids = await memory.extract_meta(user_id, days=2)
    
    if multi_day_meta_ids:
        print(f"✅ Successfully extracted {len(multi_day_meta_ids)} multi-day meta memory pieces")
    else:
        print("❌ No multi-day meta memories were extracted")
    
    print("\n" + "="*70)
    print("🔍 FINAL COMPREHENSIVE RETRIEVAL TEST")
    print("="*70)
    
    # Test comprehensive queries that should surface meta insights
    comprehensive_queries = [
        "Tell me about Alice's overall patterns and lifestyle",
        "What can you infer about Alice's work-life balance approach?",
        "Alice's social and professional activities",
        "How does Alice handle stress and maintain wellness?"
    ]
    
    for i, query in enumerate(comprehensive_queries, 1):
        print(f"\n{i}. Comprehensive Query: '{query}'")
        memories = await memory.retrieve(user_id, query, limit=5)
        
        if memories:
            meta_count = sum(1 for m in memories if m['metadata']['memory_type'] == 'meta')
            print(f"   Found {len(memories)} memories ({meta_count} meta, {len(memories)-meta_count} original)")
            
            for j, mem in enumerate(memories, 1):
                memory_type = mem['metadata']['memory_type']
                source = mem['metadata'].get('source', 'original')
                days_covered = mem['metadata'].get('days_covered', 'N/A')
                
                if memory_type == 'meta':
                    print(f"   🌟 {j}. [META] {source} (covers {days_covered} days)")
                    print(f"       Insight: {mem['content']}")
                    print(f"       Created: {mem['metadata']['created_at'][:19]}")
                    print()
                else:
                    print(f"   📄 {j}. [{memory_type.upper()}] Original memory")
                    print(f"       Content: {mem['content'][:80]}...")
                    print()
        else:
            print("   No relevant memories found.")
    
    print("\n" + "="*70)
    print("✨ META MEMORY DEMO COMPLETED SUCCESSFULLY!")
    print("="*70)
    print(f"""
📋 Demo Summary:
- Stored {len(stored_memory_ids)} diverse memories covering different aspects of user life
- Added {len(additional_memories)} follow-up memories
- Extracted meta memory for 1-day period: {len(meta_memory_ids) if meta_memory_ids else 0} pieces
- Extracted meta memory for 2-day period: {len(multi_day_meta_ids) if multi_day_meta_ids else 0} pieces
- Demonstrated how meta memories provide high-level insights and patterns

🔧 Key Features Demonstrated:
✅ Automatic extraction of behavioral patterns and insights
✅ High-level summaries derived from multiple memory types
✅ Time-period flexibility (1 day vs multi-day extraction)
✅ Enhanced semantic retrieval with meta-level understanding
✅ Integration of meta insights with original memories in search results

🎯 Meta Memory Benefits:
🧠 Provides contextual understanding of user patterns
📊 Identifies trends and behavioral insights
🎨 Enhances personalization capabilities
⚡ Improves retrieval relevance with high-level context
🔄 Creates meaningful connections between disparate memories

🚀 Use Cases for Meta Memory:
- Personalized recommendations based on lifestyle patterns
- Context-aware assistance that understands user habits
- Trend analysis for behavioral insights
- Enhanced conversation continuity across sessions
- Proactive suggestions based on identified patterns

🎭 Types of Meta Insights Generated:
- Lifestyle and activity patterns
- Work-life balance approaches
- Social interaction preferences
- Stress management strategies
- Professional development interests
- Personal growth trajectories
    """)
    
    # Show example of how to retrieve specific meta memories
    print("\n" + "="*70)
    print("📚 BONUS: RETRIEVING ONLY META MEMORIES")
    print("="*70)
    
    print("\nTo retrieve only meta memories, you can filter by memory_type...")
    all_memories = await memory.retrieve(user_id, "Alice patterns insights lifestyle", limit=10)
    meta_only = [m for m in all_memories if m['metadata']['memory_type'] == 'meta']
    
    if meta_only:
        print(f"Found {len(meta_only)} meta memories:")
        for i, meta_mem in enumerate(meta_only, 1):
            source = meta_mem['metadata'].get('source', 'unknown')
            days = meta_mem['metadata'].get('days_covered', 'N/A')
            print(f"\n{i}. Meta Insight ({source}, {days} days):")
            print(f"   {meta_mem['content']}")
    else:
        print("No meta memories found in results.")

if __name__ == "__main__":
    asyncio.run(main())
