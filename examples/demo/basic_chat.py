"""
Basic Async Chat Example

This example demonstrates the basic usage of xAgent with async chat functionality.
"""

import asyncio
from xagent.core import Agent
from xagent.tools import web_search

async def main():
    # Create agent with async-aware architecture
    agent = Agent(
        name="my_assistant",
        system_prompt="You are a helpful AI assistant.",
        model="gpt-4.1-mini",
        tools=[web_search]  # Add web search tool
    )

    # Async chat interaction with user_id and session_id
    response = await agent.chat(
        user_message="Hello, how are you?", 
        user_id="user123",
        session_id="session456"
    )
    print(response)

    # Continue conversation with context using the same user_id and session_id
    response = await agent.chat(
        user_message="What's the weather like in Hangzhou?", 
        user_id="user123",
        session_id="session456"
    )
    print(response)

    # with Image source
    response = await agent.chat(
        user_message="Analyze this image for me.",
        user_id="user123",
        session_id="session_single_image",
        image_source="https://upload.wikimedia.org/wikipedia/commons/thumb/a/a7/Atlantic_near_Faroe_Islands.jpg/960px-Atlantic_near_Faroe_Islands.jpg"  # Example image URL
    )

    print(response)

    # with multiple images
    response = await agent.chat(
        user_message="Analyze these images for me.",
        user_id="user123",
        session_id="session_multiple_images",
        image_source=["https://upload.wikimedia.org/wikipedia/commons/thumb/a/a7/Atlantic_near_Faroe_Islands.jpg/960px-Atlantic_near_Faroe_Islands.jpg", "https://upload.wikimedia.org/wikipedia/commons/thumb/4/48/Augustine_volcano_Jan_24_2006_-_Cyrus_Read.jpg/960px-Augustine_volcano_Jan_24_2006_-_Cyrus_Read.jpg"]  # Example image URLs
    )

    print(response)

# Run the async function
if __name__ == "__main__":
    asyncio.run(main())
