
import requests
from utils.tool_decorator import function_tool

API_BASE_URL = "http://localhost:8000"  # 根据实际 FastAPI 服务地址调整

@function_tool()
def lookup_word(word: str, user_id: str) -> str:
    """
    when user lookup a word or want to know the meaning of a word, use this tool
    """
    try:
        url = f"{API_BASE_URL}/lookup"
        payload = {
            "word": word,
            "user_id": user_id,
            "cache": True
        }
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.text  # 返回原始 JSON 字符串
    except Exception as e:
        return f"Error occurred while fetching word details: {str(e)}"

@function_tool()
def get_vocabulary(user_id: str, n: int = 10) -> str:
    """
    when user want to get vocabulary list for review or practice, use this tool
    """
    try:
        url = f"{API_BASE_URL}/get_vocabulary"
        payload = {
            "user_id": user_id,
            "n": n,
            "exclude_known": True
        }
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.text  # 返回原始 JSON 字符串
    except Exception as e:
        return f"Error occurred while fetching vocabulary: {str(e)}"

