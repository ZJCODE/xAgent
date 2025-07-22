import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_lookup_word_basic():
    response = client.post("/lookup", json={"word": "example"})
    assert response.status_code == 200
    data = response.json()
    assert "explanation" in data
    assert "example_sentences" in data
    assert "difficulty_level" in data

def test_lookup_word_with_user_and_save():
    response = client.post(
        "/lookup",
        json={"word": "test", "user_id": "user1", "save": True, "extra": {"tag": "pytest"}}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["word"].lower() == "test"
    assert "explanation" in data
    assert "difficulty_level" in data

@pytest.mark.parametrize("word", ["instance", "python", "data"])
def test_lookup_various_words(word):
    response = client.post("/lookup", json={"word": word})
    assert response.status_code == 200
    data = response.json()
    assert data["word"].lower() == word.lower()
    assert "explanation" in data


def test_lookup_word_empty():
    response = client.post("/lookup", json={"word": ""})
    assert response.status_code == 400
    assert response.json()["detail"] == "Word cannot be empty or None"

def test_lookup_word_none():
    response = client.post("/lookup", json={})
    assert response.status_code == 422  # FastAPI/Pydantic 校验 word 必填

def test_lookup_word_anonymous_save():
    response = client.post("/lookup", json={"word": "anonymous", "save": True})
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "anonymous"
    assert "word" in data

def test_lookup_word_extra_field():
    # extra 字段全部转为字符串
    response = client.post("/lookup", json={"word": "extra", "user_id": "user_extra", "save": True, "extra": {"tag": "pytest_extra", "level": "2"}})
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "user_extra"
    assert "extra" in data
    assert data["extra"].get("tag") == "pytest_extra"
    assert data["extra"].get("level") == "2"

def test_lookup_word_cache():
    # 第一次查词
    response1 = client.post("/lookup", json={"word": "cache", "user_id": "user_cache", "save": True})
    assert response1.status_code == 200
    data1 = response1.json()
    # 第二次查同一词，应该命中缓存/数据库，word/user_id 一致即可
    response2 = client.post("/lookup", json={"word": "cache", "user_id": "user_cache", "save": True})
    assert response2.status_code == 200
    data2 = response2.json()
    assert data1["word"] == data2["word"]
    assert data1["user_id"] == data2["user_id"]
    assert "explanation" in data2

def test_lookup_word_disable_cache():
    # 查词时 cache=False，理论上每次都走 LLM，但接口应能正常返回
    response = client.post("/lookup", json={"word": "no", "user_id": "user_no_cache", "cache": False})
    assert response.status_code == 200
    data = response.json()
    assert "word" in data
