#!/usr/bin/env python3
"""
Advanced HTTP Agent Call Example

This example demonstrates how to use the new history_count and max_iter parameters
when calling the xAgent HTTP server.
"""

import requests
import json
import time


def chat_with_agent(user_message, user_id="test", session_id="test", 
                   image_source=None, history_count=None, max_iter=None, 
                   stream=False, base_url="http://localhost:8010"):
    """
    Chat with the agent via HTTP API.
    
    Args:
        user_message (str): The user's message
        user_id (str): Unique identifier for the user
        session_id (str): Unique identifier for the conversation session
        image_source (str, optional): Image URL, file path, or base64 string
        history_count (int, optional): Number of previous messages to include (default: 16)
        max_iter (int, optional): Maximum model call attempts (default: 10)
        stream (bool): Whether to enable streaming response
        base_url (str): Base URL of the HTTP agent server
        
    Returns:
        str or dict: Agent response
    """
    url = f"{base_url}/chat"
    
    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "user_message": user_message,
        "stream": stream
    }
    
    # Add optional parameters
    if image_source:
        payload["image_source"] = image_source
    if history_count is not None:
        payload["history_count"] = history_count
    if max_iter is not None:
        payload["max_iter"] = max_iter
    
    print(f"Sending request to {url}")
    print(f"Payload: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        result = response.json()["reply"]
        print(f"Response: {result}\n")
        return result
    else:
        error_msg = f"Error {response.status_code}: {response.text}"
        print(f"Error: {error_msg}\n")
        return error_msg


def clear_session(user_id, session_id, base_url="http://localhost:8010"):
    """Clear the conversation session."""
    url = f"{base_url}/clear_session"
    payload = {
        "user_id": user_id,
        "session_id": session_id
    }
    
    response = requests.post(url, json=payload)
    if response.status_code == 200:
        print(f"Session cleared: {response.json()}")
    else:
        print(f"Failed to clear session: {response.status_code}")


def main():
    """Demonstrate various usage scenarios."""
    
    print("=== xAgent HTTP API Advanced Examples ===\n")
    
    session_id = f"test_session_{int(time.time())}"
    user_id = "demo_user"
    
    # Example 1: Basic conversation
    print("1. Basic conversation:")
    chat_with_agent("你好，我是张三，请记住我的名字", 
                   user_id=user_id, session_id=session_id)
    
    # Example 2: Continue conversation with default parameters
    print("2. Continue conversation (default parameters):")
    chat_with_agent("我刚才告诉你我的名字是什么？", 
                   user_id=user_id, session_id=session_id)
    
    # Example 3: Use custom history_count 
    print("3. Limited history (history_count=1):")
    chat_with_agent("我的名字是什么？", 
                   user_id=user_id, session_id=session_id, 
                   history_count=1)  # Only include 1 previous message
    
    # Example 4: Use custom max_iter for complex tasks
    print("4. Complex task with more iterations (max_iter=15):")
    chat_with_agent("请帮我分析一下人工智能在未来10年的发展趋势，并给出详细的预测", 
                   user_id=user_id, session_id=session_id, 
                   max_iter=15)  # Allow more model iterations for complex reasoning
    
    # Example 5: Use both custom parameters
    print("5. Custom history and iterations:")
    chat_with_agent("基于我们之前的所有对话，总结一下你对我的了解", 
                   user_id=user_id, session_id=session_id,
                   history_count=20,  # Include more history
                   max_iter=12)       # More iterations for comprehensive response
    
    # Clean up
    print("6. Cleaning up session:")
    clear_session(user_id, session_id)


if __name__ == "__main__":
    main()
