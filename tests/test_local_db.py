#!/usr/bin/env python3
"""
Tests for LocalDB implementation.
"""

import pytest
import asyncio
from xagent.db.local_messages import LocalDB
from xagent.core.session import Session
from xagent.schemas.message import Message


class TestLocalDB:
    """Test cases for LocalDB class."""
    
    @pytest.fixture
    def local_db(self):
        """Create a fresh LocalDB instance for each test."""
        return LocalDB()
    
    @pytest.fixture
    def sample_messages(self):
        """Create sample messages for testing."""
        return [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi there!"),
            Message(role="user", content="How are you?")
        ]
    
    @pytest.mark.asyncio
    async def test_add_and_get_messages(self, local_db, sample_messages):
        """Test adding and retrieving messages."""
        user_id = "test_user"
        session_id = "test_session"
        
        # Add messages
        await local_db.add_messages(user_id, sample_messages, session_id)
        
        # Get all messages
        retrieved = await local_db.get_messages(user_id, session_id, 10)
        
        assert len(retrieved) == 3
        assert retrieved[0].content == "Hello"
        assert retrieved[1].content == "Hi there!"
        assert retrieved[2].content == "How are you?"
    
    @pytest.mark.asyncio
    async def test_get_messages_with_limit(self, local_db, sample_messages):
        """Test retrieving messages with count limit."""
        user_id = "test_user"
        session_id = "test_session"
        
        await local_db.add_messages(user_id, sample_messages, session_id)
        
        # Get last 2 messages
        retrieved = await local_db.get_messages(user_id, session_id, 2)
        
        assert len(retrieved) == 2
        assert retrieved[0].content == "Hi there!"
        assert retrieved[1].content == "How are you?"
    
    @pytest.mark.asyncio
    async def test_clear_history(self, local_db, sample_messages):
        """Test clearing session history."""
        user_id = "test_user"
        session_id = "test_session"
        
        await local_db.add_messages(user_id, sample_messages, session_id)
        await local_db.clear_history(user_id, session_id)
        
        retrieved = await local_db.get_messages(user_id, session_id, 10)
        assert len(retrieved) == 0
    
    @pytest.mark.asyncio
    async def test_pop_message(self, local_db):
        """Test popping messages."""
        user_id = "test_user"
        session_id = "test_session"
        
        messages = [
            Message(role="user", content="Message 1"),
            Message(role="assistant", content="Message 2"),
        ]
        
        await local_db.add_messages(user_id, messages, session_id)
        
        # Pop last message
        popped = await local_db.pop_message(user_id, session_id)
        assert popped.content == "Message 2"
        
        # Check remaining messages
        remaining = await local_db.get_messages(user_id, session_id, 10)
        assert len(remaining) == 1
        assert remaining[0].content == "Message 1"
    
    @pytest.mark.asyncio
    async def test_message_count(self, local_db, sample_messages):
        """Test message counting."""
        user_id = "test_user"
        session_id = "test_session"
        
        # Initially empty
        count = await local_db.get_message_count(user_id, session_id)
        assert count == 0
        
        # After adding messages
        await local_db.add_messages(user_id, sample_messages, session_id)
        count = await local_db.get_message_count(user_id, session_id)
        assert count == 3
    
    @pytest.mark.asyncio
    async def test_has_messages(self, local_db, sample_messages):
        """Test has_messages method."""
        user_id = "test_user"
        session_id = "test_session"
        
        # Initially empty
        has_msgs = await local_db.has_messages(user_id, session_id)
        assert not has_msgs
        
        # After adding messages
        await local_db.add_messages(user_id, sample_messages, session_id)
        has_msgs = await local_db.has_messages(user_id, session_id)
        assert has_msgs
    
    def test_session_isolation(self, local_db):
        """Test that different sessions are isolated."""
        asyncio.run(self._test_session_isolation_async(local_db))
    
    async def _test_session_isolation_async(self, local_db):
        """Async helper for session isolation test."""
        user1_msg = Message(role="user", content="User 1 message")
        user2_msg = Message(role="user", content="User 2 message")
        
        await local_db.add_messages("user1", user1_msg, "session1")
        await local_db.add_messages("user2", user2_msg, "session1")
        
        user1_messages = await local_db.get_messages("user1", "session1", 10)
        user2_messages = await local_db.get_messages("user2", "session1", 10)
        
        assert len(user1_messages) == 1
        assert len(user2_messages) == 1
        assert user1_messages[0].content == "User 1 message"
        assert user2_messages[0].content == "User 2 message"
    
    def test_get_all_sessions(self, local_db):
        """Test getting all session keys."""
        asyncio.run(self._test_get_all_sessions_async(local_db))
    
    async def _test_get_all_sessions_async(self, local_db):
        """Async helper for get all sessions test."""
        msg = Message(role="user", content="Test")
        
        await local_db.add_messages("user1", msg, "session1")
        await local_db.add_messages("user1", msg, "session2")
        await local_db.add_messages("user2", msg, "session1")
        
        sessions = local_db.get_all_sessions()
        assert len(sessions) == 3
        assert ("user1", "session1") in sessions
        assert ("user1", "session2") in sessions
        assert ("user2", "session1") in sessions


class TestSessionWithLocalDB:
    """Test Session class with LocalDB integration."""
    
    @pytest.fixture
    def sample_messages(self):
        """Create sample messages for testing."""
        return [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi there!")
        ]
    
    @pytest.mark.asyncio
    async def test_session_with_default_local_db(self, sample_messages):
        """Test Session with default LocalDB."""
        session = Session(user_id="test_user", session_id="test_session")
        
        await session.add_messages(sample_messages)
        retrieved = await session.get_messages()
        
        assert len(retrieved) == 2
        assert retrieved[0].content == "Hello"
        assert retrieved[1].content == "Hi there!"
    
    @pytest.mark.asyncio
    async def test_session_with_custom_local_db(self, sample_messages):
        """Test Session with custom LocalDB instance."""
        local_db = LocalDB()
        session = Session(user_id="test_user", session_id="test_session", local_db=local_db)
        
        await session.add_messages(sample_messages)
        retrieved = await session.get_messages()
        
        assert len(retrieved) == 2
        
        # Verify it's using our LocalDB instance
        direct_messages = await local_db.get_messages("test_user", "test_session", 10)
        assert len(direct_messages) == 2
    
    @pytest.mark.asyncio
    async def test_shared_local_db_between_sessions(self):
        """Test sharing LocalDB between multiple sessions."""
        shared_db = LocalDB()
        
        session1 = Session(user_id="user1", session_id="chat", local_db=shared_db)
        session2 = Session(user_id="user2", session_id="chat", local_db=shared_db)
        
        await session1.add_messages(Message(role="user", content="User 1 message"))
        await session2.add_messages(Message(role="user", content="User 2 message"))
        
        # Check that both sessions exist in the shared DB
        sessions = Session.get_all_local_sessions(shared_db)
        assert len(sessions) == 2
        assert ("user1", "chat") in sessions
        assert ("user2", "chat") in sessions
    
    def test_session_info_with_local_db(self):
        """Test session info with LocalDB backend."""
        session = Session(user_id="test_user", session_id="test_session")
        info = session.get_session_info()
        
        assert info["user_id"] == "test_user"
        assert info["session_id"] == "test_session"
        assert info["backend"] == "local"


if __name__ == "__main__":
    # Run basic tests
    async def run_basic_tests():
        """Run some basic tests manually."""
        print("Running basic LocalDB tests...")
        
        # Test LocalDB directly
        db = LocalDB()
        msg = Message(role="user", content="Test message")
        
        await db.add_messages("user1", msg, "session1")
        messages = await db.get_messages("user1", "session1", 10)
        
        assert len(messages) == 1
        assert messages[0].content == "Test message"
        print("✓ LocalDB basic test passed")
        
        # Test Session with LocalDB
        session = Session(user_id="user2", session_id="session2")
        await session.add_messages(Message(role="user", content="Session test"))
        
        retrieved = await session.get_messages()
        assert len(retrieved) == 1
        assert retrieved[0].content == "Session test"
        print("✓ Session with LocalDB test passed")
        
        print("All basic tests passed!")
    
    asyncio.run(run_basic_tests())
