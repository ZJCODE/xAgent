import requests
import json

def chat_with_agent(user_message, user_id="test", session_id="test", image_source=None):
    url = "http://localhost:8010/chat"
    
    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "user_message": user_message
    }
    
    if image_source:
        payload["image_source"] = image_source
    
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        return response.json()["reply"]
    else:
        return f"Error: {response.status_code}"

# Usage
reply = chat_with_agent("你是谁")
print(reply)

# Continue conversation with context
reply = chat_with_agent("我的名字是张三", session_id="session456")
print(reply)

reply = chat_with_agent("我的名字是什么？", session_id="session456")
print(reply)  # Will remember the name from previous message