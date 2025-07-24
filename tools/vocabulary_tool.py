import httpx
from utils.tool_decorator import function_tool

API_BASE_URL = "http://localhost:8000"  # 根据实际 FastAPI 服务地址调整

@function_tool()
async def lookup_word(word: str, user_id: str) -> str:
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
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.text  # 返回原始 JSON 字符串
    except Exception as e:
        return f"Error occurred while fetching word details: {str(e)}"

@function_tool()
async def get_vocabulary(user_id: str, n: int = 10) -> str:
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
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.text  # 返回原始 JSON 字符串
    except Exception as e:
        return f"Error occurred while fetching vocabulary: {str(e)}"


if __name__ == "__main__":
    import asyncio
    # Example usage
    user_id = "test_user"
    word = "example"
    
    # Lookup a word
    result = asyncio.run(lookup_word(word, user_id))
    print("Lookup Result:", result)
    
    # Get vocabulary list
    vocab_result = asyncio.run(get_vocabulary(user_id, 5))
    print("Vocabulary List:", vocab_result)