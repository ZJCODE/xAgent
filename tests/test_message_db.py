import os
import pytest
import time
from dotenv import load_dotenv

load_dotenv(override=True)

from xagent.schemas import Message
from xagent.db import MessageDB

@pytest.fixture(scope="function")
def message_db():
    """Fixture: 每个测试用例前后清理 Redis，保证测试隔离。"""
    db = MessageDB(os.environ.get("TEST_REDIS_URL"))
    # 清理所有以 chat: 开头的 key
    for key in db.r.keys("chat:*"):
        db.r.delete(key)
    yield db
    for key in db.r.keys("chat:*"):
        db.r.delete(key)

def test_add_and_get_message(message_db):
    """测试添加和获取消息功能。"""
    user_id = "user1"
    msg1 = Message(role="user", content="Hello", timestamp=time.time())
    msg2 = Message(role="assistant", content="Hi!", timestamp=time.time())
    message_db.add_message(user_id, msg1)
    message_db.add_message(user_id, msg2)
    msgs = message_db.get_messages(user_id, count=2)
    assert len(msgs) == 2
    assert msgs[0].content == "Hello"
    assert msgs[1].role == "assistant"

def test_add_message_with_session(message_db):
    """测试带 session_id 的消息存储和获取。"""
    user_id = "user2"
    session_id = "sessA"
    msg = Message(role="user", content="Session test", timestamp=time.time())
    message_db.add_message(user_id, msg, session_id=session_id)
    msgs = message_db.get_messages(user_id, session_id=session_id)
    assert len(msgs) == 1
    assert msgs[0].content == "Session test"

def test_trim_history(message_db):
    """测试消息历史裁剪功能。"""
    user_id = "user3"
    for i in range(10):
        msg = Message(role="user", content=f"msg{i}", timestamp=time.time())
        message_db.add_message(user_id, msg)
    message_db.trim_history(user_id, max_len=5)
    msgs = message_db.get_messages(user_id, count=10)
    assert len(msgs) == 5
    assert msgs[0].content == "msg5"
    assert msgs[-1].content == "msg9"

def test_set_expire(message_db):
    """测试设置过期时间。"""
    user_id = "user4"
    msg = Message(role="user", content="expire test", timestamp=time.time())
    message_db.add_message(user_id, msg, ttl=10)
    message_db.set_expire(user_id, ttl=1)
    key = message_db._make_key(user_id)
    ttl = message_db.r.ttl(key)
    assert 0 < ttl <= 1

def test_get_messages_empty(message_db):
    """测试获取空消息历史。"""
    user_id = "user_empty"
    msgs = message_db.get_messages(user_id)
    assert msgs == []
    assert isinstance(msgs, list)

def test_message_db_init_without_url(monkeypatch):
    """测试未设置 REDIS_URL 时抛出异常。"""
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(ValueError):
        MessageDB(redis_url=None)
