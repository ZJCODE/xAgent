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
