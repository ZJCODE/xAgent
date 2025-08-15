import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from typing import List

from xagent.core.session import Session, SessionConfig
from xagent.schemas.message import Message, ToolCall
from xagent.db import MessageStorageBase, MessageStorageLocal


class TestSessionConfig:
    """Test SessionConfig constants."""
    
    def test_default_values(self):
        """Test that default configuration values are correct."""
        assert SessionConfig.DEFAULT_USER_ID == "default_user"
        assert SessionConfig.DEFAULT_SESSION_ID == "default_session"
        assert SessionConfig.DEFAULT_MESSAGE_COUNT == 20
        assert SessionConfig.MAX_LOCAL_HISTORY == 100


class TestSession:
    """Test Session class functionality."""
    
    def setup_method(self):
        """Set up test fixtures before each test method."""
        # Create test messages
        self.test_message_1 = Message(
            role="user",
            content="Hello, how are you?"
        )
        self.test_message_2 = Message(
            role="assistant", 
            content="I'm doing well, thank you!"
        )
        self.test_tool_message = Message(
            role="assistant",
            content="Tool call result",
            tool_call=ToolCall(
                call_id="test_call_1",
                name="test_tool",
                arguments='{"param": "value"}',
                output="Tool executed successfully"
            )
        )
    
    def teardown_method(self):
        """Clean up after each test method."""
        # No need to clear anything since Session now uses MessageStorageLocal by default
        pass

    def test_init_with_defaults(self):
        """Test Session initialization with default values."""
        session = Session()
        
        assert session.user_id == SessionConfig.DEFAULT_USER_ID
        assert session.session_id == SessionConfig.DEFAULT_SESSION_ID
        assert isinstance(session.message_storage, MessageStorageLocal)
        assert session.logger.name.startswith("Session[default_user:default_session]")

    def test_init_with_custom_values(self):
        """Test Session initialization with custom values."""
        mock_storage = MagicMock(spec=MessageStorageBase)
        session = Session(
            user_id="test_user",
            session_id="test_session",
            message_storage=mock_storage
        )
        
        assert session.user_id == "test_user"
        assert session.session_id == "test_session"
        assert session.message_storage is mock_storage
        assert session.logger.name.startswith("Session[test_user:test_session]")

    def test_normalize_messages_input(self):
        """Test message input normalization."""
        session = Session()
        
        # Test single message
        normalized = session._normalize_messages_input(self.test_message_1)
        assert normalized == [self.test_message_1]
        
        # Test list of messages
        message_list = [self.test_message_1, self.test_message_2]
        normalized = session._normalize_messages_input(message_list)
        assert normalized == message_list

    @pytest.mark.asyncio
    async def test_add_single_message_local(self):
        """Test adding a single message to local storage."""
        session = Session(user_id="user1", session_id="session1")
        
        await session.add_messages(self.test_message_1)
        
        messages = await session.get_messages()
        assert len(messages) == 1
        assert messages[0].content == "Hello, how are you?"
        assert messages[0].role == "user"

    @pytest.mark.asyncio
    async def test_add_multiple_messages_local(self):
        """Test adding multiple messages to local storage."""
        session = Session(user_id="user1", session_id="session1")
        messages_to_add = [self.test_message_1, self.test_message_2]
        
        await session.add_messages(messages_to_add)
        
        retrieved_messages = await session.get_messages()
        assert len(retrieved_messages) == 2
        assert retrieved_messages[0].content == "Hello, how are you?"
        assert retrieved_messages[1].content == "I'm doing well, thank you!"

    @pytest.mark.asyncio
    async def test_add_messages_with_custom_storage(self):
        """Test adding messages with custom storage backend."""
        mock_storage = AsyncMock(spec=MessageStorageBase)
        session = Session(
            user_id="user1", 
            session_id="session1",
            message_storage=mock_storage
        )
        
        await session.add_messages(self.test_message_1)
        
        mock_storage.add_messages.assert_called_once_with(
            "user1", 
            "session1",
            [self.test_message_1]
        )

    @pytest.mark.asyncio
    async def test_get_messages_local(self):
        """Test retrieving messages from local storage."""
        session = Session(user_id="user1", session_id="session1")
        messages_to_add = [self.test_message_1, self.test_message_2]
        
        await session.add_messages(messages_to_add)
        
        # Test getting all messages
        all_messages = await session.get_messages(10)
        assert len(all_messages) == 2
        
        # Test getting limited messages
        limited_messages = await session.get_messages(1)
        assert len(limited_messages) == 1
        assert limited_messages[0].content == "I'm doing well, thank you!"

    @pytest.mark.asyncio
    async def test_get_messages_with_custom_storage(self):
        """Test retrieving messages with custom storage backend."""
        mock_storage = AsyncMock(spec=MessageStorageBase)
        mock_storage.get_messages.return_value = [self.test_message_1, self.test_message_2]
        
        session = Session(
            user_id="user1",
            session_id="session1", 
            message_storage=mock_storage
        )
        
        messages = await session.get_messages(5)
        
        mock_storage.get_messages.assert_called_once_with("user1", "session1", 5)
        assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_get_messages_invalid_count(self):
        """Test that invalid message count raises ValueError."""
        session = Session()
        
        with pytest.raises(ValueError, match="Message count must be positive"):
            await session.get_messages(0)
        
        with pytest.raises(ValueError, match="Message count must be positive"):
            await session.get_messages(-1)

    @pytest.mark.asyncio
    async def test_clear_session_local(self):
        """Test clearing session with local storage."""
        session = Session(user_id="user1", session_id="session1")
        
        # Add messages
        await session.add_messages([self.test_message_1, self.test_message_2])
        assert len(await session.get_messages()) == 2
        
        # Clear session
        await session.clear_session()
        
        # Verify session is empty
        messages = await session.get_messages()
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_clear_session_with_custom_storage(self):
        """Test clearing session with custom storage backend."""
        mock_storage = AsyncMock(spec=MessageStorageBase)
        session = Session(
            user_id="user1",
            session_id="session1",
            message_storage=mock_storage
        )
        
        await session.clear_session()
        
        mock_storage.clear_history.assert_called_once_with("user1", "session1")

    @pytest.mark.asyncio
    async def test_pop_message_local(self):
        """Test popping messages from local storage."""
        session = Session(user_id="user1", session_id="session1")
        
        # Add messages including a tool message at the end
        await session.add_messages([
            self.test_message_1,       # "Hello, how are you?"
            self.test_message_2,       # "I'm doing well, thank you!"
            self.test_tool_message     # Tool message (will be skipped)
        ])
        
        # Pop should skip tool message and return the last non-tool message
        popped = await session.pop_message()
        assert popped.content == "I'm doing well, thank you!"
        
        # Verify that both the tool message and the returned message were removed
        remaining = await session.get_messages()
        assert len(remaining) == 1  # Only the first message should remain
        assert remaining[0].content == "Hello, how are you?"

    @pytest.mark.asyncio
    async def test_pop_message_with_custom_storage(self):
        """Test popping message with custom storage backend."""
        mock_storage = AsyncMock(spec=MessageStorageBase)
        mock_storage.pop_message.return_value = self.test_message_1
        
        session = Session(
            user_id="user1",
            session_id="session1",
            message_storage=mock_storage
        )
        
        popped = await session.pop_message()
        
        mock_storage.pop_message.assert_called_once_with("user1", "session1")
        assert popped == self.test_message_1

    @pytest.mark.asyncio
    async def test_pop_message_empty_session(self):
        """Test popping from empty session returns None."""
        session = Session(user_id="user1", session_id="session1")
        
        popped = await session.pop_message()
        assert popped is None

    @pytest.mark.asyncio
    async def test_get_message_count_local(self):
        """Test getting message count with local storage."""
        session = Session(user_id="user1", session_id="session1")
        
        # Initially empty
        count = await session.get_message_count()
        assert count == 0
        
        # Add messages
        await session.add_messages([self.test_message_1, self.test_message_2])
        
        count = await session.get_message_count()
        assert count == 2

    @pytest.mark.asyncio
    async def test_get_message_count_with_custom_storage(self):
        """Test getting message count with custom storage backend."""
        mock_storage = AsyncMock(spec=MessageStorageBase)
        mock_storage.get_message_count.return_value = 2
        
        session = Session(
            user_id="user1",
            session_id="session1",
            message_storage=mock_storage
        )
        
        count = await session.get_message_count()
        assert count == 2

    @pytest.mark.asyncio
    async def test_has_messages(self):
        """Test checking if session has messages."""
        session = Session(user_id="user1", session_id="session1")
        
        # Initially empty
        has_messages = await session.has_messages()
        assert not has_messages
        
        # Add a message
        await session.add_messages(self.test_message_1)
        
        has_messages = await session.has_messages()
        assert has_messages

    def test_get_session_info(self):
        """Test getting session information."""
        # Test with local backend
        session = Session(user_id="user1", session_id="session1")
        info = session.get_session_info()
        
        expected_keys = {"user_id", "session_id", "backend", "session_key"}
        assert set(info.keys()).issuperset(expected_keys)
        assert info["user_id"] == "user1"
        assert info["session_id"] == "session1"
        assert info["backend"] == "local"
        assert info["session_key"] == "user1:session1"
        
        # Test with custom storage backend
        mock_storage = MagicMock(spec=MessageStorageBase)
        mock_storage.get_session_info.return_value = {
            "user_id": "user2",
            "session_id": "session2",
            "backend": "custom", 
            "session_key": "user2:session2"
        }
        session_custom = Session(
            user_id="user2",
            session_id="session2",
            message_storage=mock_storage
        )
        info_custom = session_custom.get_session_info()
        
        assert info_custom["backend"] == "custom"

    @pytest.mark.asyncio
    async def test_multiple_sessions_isolation(self):
        """Test that different sessions are properly isolated."""
        session1 = Session(user_id="user1", session_id="session1")
        session2 = Session(user_id="user2", session_id="session2")
        
        # Add different messages to each session
        await session1.add_messages(self.test_message_1)
        await session2.add_messages(self.test_message_2)
        
        # Verify sessions are isolated
        messages1 = await session1.get_messages()
        messages2 = await session2.get_messages()
        
        assert len(messages1) == 1
        assert len(messages2) == 1
        assert messages1[0].content != messages2[0].content
        assert messages1[0].content == "Hello, how are you?"
        assert messages2[0].content == "I'm doing well, thank you!"

    def test_str_and_repr(self):
        """Test string representations of Session."""
        session = Session(user_id="test_user", session_id="test_session")
        
        # Test __str__
        str_repr = str(session)
        assert str_repr == "Session(user_id='test_user', session_id='test_session')"
        
        # Test __repr__ with local backend
        repr_str = repr(session)
        assert "Session(user_id='test_user', session_id='test_session'" in repr_str
        assert "MessageStorageLocal" in repr_str
        
        # Test __repr__ with custom storage backend
        mock_storage = MagicMock(spec=MessageStorageBase)
        session_custom = Session(
            user_id="test_user",
            session_id="test_session", 
            message_storage=mock_storage
        )
        repr_str_custom = repr(session_custom)
        assert "Session(user_id='test_user', session_id='test_session'" in repr_str_custom

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """Test error handling in various methods."""
        # Test with mock storage that raises exceptions
        mock_storage = AsyncMock(spec=MessageStorageBase)
        mock_storage.add_messages.side_effect = Exception("Storage error")
        mock_storage.get_messages.side_effect = Exception("Storage error")
        mock_storage.clear_history.side_effect = Exception("Storage error")
        mock_storage.pop_message.side_effect = Exception("Storage error")
        mock_storage.get_message_count.side_effect = Exception("Storage error")
        
        session = Session(
            user_id="user1",
            session_id="session1",
            message_storage=mock_storage
        )
        
        # Test add_messages error handling (should not raise)
        await session.add_messages(self.test_message_1)
        
        # Test get_messages error handling (should return empty list)
        messages = await session.get_messages()
        assert messages == []
        
        # Test clear_session error handling (should not raise)
        await session.clear_session()
        
        # Test pop_message error handling (should return None)
        popped = await session.pop_message()
        assert popped is None
        
        # Test get_message_count error handling (should return 0)
        count = await session.get_message_count()
        assert count == 0


class TestSessionWithMessageStorageLocal:
    """Test Session class functionality specifically with MessageStorageLocal backend."""
    
    def setup_method(self):
        """Set up test fixtures before each test method."""
        # Create test messages
        self.test_message_1 = Message(
            role="user",
            content="Hello, how are you?"
        )
        self.test_message_2 = Message(
            role="assistant", 
            content="I'm doing well, thank you!"
        )
        self.test_tool_message = Message(
            role="assistant",
            content="Tool call result",
            tool_call=ToolCall(
                call_id="test_call_1",
                name="test_tool",
                arguments='{"param": "value"}',
                output="Tool executed successfully"
            )
        )
    
    def test_local_storage_session_isolation(self):
        """Test that local storage properly isolates different sessions."""
        storage = MessageStorageLocal()
        session1 = Session(user_id="user1", session_id="session1", message_storage=storage)
        session2 = Session(user_id="user1", session_id="session2", message_storage=storage)
        session3 = Session(user_id="user2", session_id="session1", message_storage=storage)
        
        # All sessions should use the same storage instance
        assert session1.message_storage is storage
        assert session2.message_storage is storage
        assert session3.message_storage is storage
        
        # But sessions should still be isolated
        asyncio.run(session1.add_messages(self.test_message_1))
        asyncio.run(session2.add_messages(self.test_message_2))
        
        messages1 = asyncio.run(session1.get_messages())
        messages2 = asyncio.run(session2.get_messages())
        messages3 = asyncio.run(session3.get_messages())
        
        assert len(messages1) == 1
        assert len(messages2) == 1
        assert len(messages3) == 0
        assert messages1[0].content == "Hello, how are you?"
        assert messages2[0].content == "I'm doing well, thank you!"
    
    @pytest.mark.asyncio
    async def test_local_storage_history_trimming(self):
        """Test that local storage trims history when it exceeds MAX_LOCAL_HISTORY."""
        from xagent.db.local_messages import MessageStorageLocalConfig
        
        session = Session(user_id="user1", session_id="session1")
        
        # Create messages exceeding the limit
        messages = []
        for i in range(MessageStorageLocalConfig.MAX_LOCAL_HISTORY + 10):
            messages.append(Message(
                role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i}"
            ))
        
        # Add all messages at once (should trigger trimming)
        await session.add_messages(messages)
        
        # Verify only MAX_LOCAL_HISTORY messages remain
        stored_messages = await session.get_messages(999999)  # Get all messages
        assert len(stored_messages) == MessageStorageLocalConfig.MAX_LOCAL_HISTORY
        
        # Verify the last messages are kept (the most recent ones)
        assert stored_messages[-1].content == f"Message {MessageStorageLocalConfig.MAX_LOCAL_HISTORY + 9}"
        assert stored_messages[0].content == f"Message 10"  # First message after trimming
    
    @pytest.mark.asyncio
    async def test_local_storage_tool_message_handling(self):
        """Test tool message handling in local storage."""
        session = Session(user_id="user1", session_id="session1")
        
        # Add regular messages and tool message
        await session.add_messages([
            self.test_message_1,
            self.test_message_2,
            self.test_tool_message
        ])
        
        # Get all messages - should include tool message
        all_messages = await session.get_messages(10)
        assert len(all_messages) == 3
        assert all_messages[2].tool_call is not None
        
        # Pop message - should skip tool message and return last non-tool message
        popped = await session.pop_message()
        assert popped.content == "I'm doing well, thank you!"
        
        # Verify tool message was also removed during pop operation
        remaining = await session.get_messages(10)
        assert len(remaining) == 1
        assert remaining[0].content == "Hello, how are you?"
    
    def test_local_storage_session_management(self):
        """Test local storage session management methods."""
        storage = MessageStorageLocal()
        
        # Initially empty
        sessions = storage.get_all_sessions()
        assert sessions == []
        
        # Create some sessions by adding messages
        session1 = Session(user_id="user1", session_id="session1", message_storage=storage)
        session2 = Session(user_id="user2", session_id="session2", message_storage=storage)
        
        asyncio.run(session1.add_messages(self.test_message_1))
        asyncio.run(session2.add_messages(self.test_message_2))
        
        # Check sessions exist
        sessions = storage.get_all_sessions()
        assert len(sessions) == 2
        assert ("user1", "session1") in sessions
        assert ("user2", "session2") in sessions
        
        # Clear all sessions
        storage.clear_all_sessions()
        sessions = storage.get_all_sessions()
        assert sessions == []
    
    def test_local_storage_session_info(self):
        """Test local storage session info functionality."""
        session = Session(user_id="user1", session_id="session1")
        
        # Add some messages
        asyncio.run(session.add_messages([self.test_message_1, self.test_message_2]))
        
        info = session.get_session_info()
        
        assert info["user_id"] == "user1"
        assert info["session_id"] == "session1"
        assert info["backend"] == "local"
        assert info["session_key"] == "user1:session1"
        assert info["message_count"] == "2"  # Should be string representation
    
    def test_local_storage_string_representations(self):
        """Test string representations of MessageStorageLocal."""
        storage = MessageStorageLocal()
        
        # Initially empty
        str_repr = str(storage)
        assert "MessageStorageLocal" in str_repr or "LocalDB" in str_repr
        
        repr_str = repr(storage)
        assert "MessageStorageLocal" in repr_str or "LocalDB" in repr_str
        
        # Add some sessions
        session1 = Session(user_id="user1", session_id="session1", message_storage=storage)
        session2 = Session(user_id="user2", session_id="session2", message_storage=storage)
        
        asyncio.run(session1.add_messages([self.test_message_1, self.test_message_2]))
        asyncio.run(session2.add_messages(self.test_tool_message))
        
        # Check representations include session count
        str_repr_with_data = str(storage)
        repr_str_with_data = repr(storage)
        
        # Should show session information
        assert "2" in str_repr_with_data or "sessions" in str_repr_with_data.lower()
        assert "3" in repr_str_with_data or "messages" in repr_str_with_data.lower()
