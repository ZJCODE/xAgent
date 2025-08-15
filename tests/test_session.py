import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from typing import List

from xagent.core.session import Session, SessionConfig
from xagent.schemas.message import Message, ToolCall
from xagent.db.message import MessageDB


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
        # Clear any existing local messages
        Session.clear_all_local_sessions()
        
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
        Session.clear_all_local_sessions()

    def test_init_with_defaults(self):
        """Test Session initialization with default values."""
        session = Session()
        
        assert session.user_id == SessionConfig.DEFAULT_USER_ID
        assert session.session_id == SessionConfig.DEFAULT_SESSION_ID
        assert session.message_db is None
        assert session.logger.name.startswith("Session[default_user:default_session]")

    def test_init_with_custom_values(self):
        """Test Session initialization with custom values."""
        mock_db = MagicMock(spec=MessageDB)
        session = Session(
            user_id="test_user",
            session_id="test_session",
            message_db=mock_db
        )
        
        assert session.user_id == "test_user"
        assert session.session_id == "test_session"
        assert session.message_db is mock_db
        assert session.logger.name.startswith("Session[test_user:test_session]")

    def test_session_key_generation(self):
        """Test session key generation for local storage."""
        session = Session(user_id="user1", session_id="session1")
        key = session._get_session_key()
        
        assert key == ("user1", "session1")

    def test_local_session_initialization(self):
        """Test that local session storage is properly initialized."""
        session = Session(user_id="user1", session_id="session1")
        session_key = session._get_session_key()
        
        assert session_key in Session._local_messages
        assert Session._local_messages[session_key] == []

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
    async def test_add_messages_with_database(self):
        """Test adding messages with database backend."""
        mock_db = AsyncMock(spec=MessageDB)
        session = Session(
            user_id="user1", 
            session_id="session1",
            message_db=mock_db
        )
        
        await session.add_messages(self.test_message_1)
        
        mock_db.add_messages.assert_called_once_with(
            "user1", 
            [self.test_message_1], 
            "session1"
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
    async def test_get_messages_with_database(self):
        """Test retrieving messages with database backend."""
        mock_db = AsyncMock(spec=MessageDB)
        mock_db.get_messages.return_value = [self.test_message_1, self.test_message_2]
        
        session = Session(
            user_id="user1",
            session_id="session1", 
            message_db=mock_db
        )
        
        messages = await session.get_messages(5)
        
        mock_db.get_messages.assert_called_once_with("user1", "session1", 5)
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
    async def test_clear_session_with_database(self):
        """Test clearing session with database backend."""
        mock_db = AsyncMock(spec=MessageDB)
        session = Session(
            user_id="user1",
            session_id="session1",
            message_db=mock_db
        )
        
        await session.clear_session()
        
        mock_db.clear_history.assert_called_once_with("user1", "session1")

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
    async def test_pop_message_with_database(self):
        """Test popping message with database backend."""
        mock_db = AsyncMock(spec=MessageDB)
        mock_db.pop_message.return_value = self.test_message_1
        
        session = Session(
            user_id="user1",
            session_id="session1",
            message_db=mock_db
        )
        
        popped = await session.pop_message()
        
        mock_db.pop_message.assert_called_once_with("user1", "session1")
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
    async def test_get_message_count_with_database(self):
        """Test getting message count with database backend."""
        mock_db = AsyncMock(spec=MessageDB)
        mock_db.get_messages.return_value = [self.test_message_1, self.test_message_2]
        
        session = Session(
            user_id="user1",
            session_id="session1",
            message_db=mock_db
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
        
        expected = {
            "user_id": "user1",
            "session_id": "session1", 
            "backend": "local",
            "session_key": "user1:session1"
        }
        assert info == expected
        
        # Test with database backend
        mock_db = MagicMock(spec=MessageDB)
        session_db = Session(
            user_id="user2",
            session_id="session2",
            message_db=mock_db
        )
        info_db = session_db.get_session_info()
        
        expected_db = {
            "user_id": "user2",
            "session_id": "session2",
            "backend": "database", 
            "session_key": "user2:session2"
        }
        assert info_db == expected_db

    def test_get_all_local_sessions(self):
        """Test getting all local session keys."""
        # Initially empty
        sessions = Session.get_all_local_sessions()
        assert sessions == []
        
        # Create sessions
        session1 = Session(user_id="user1", session_id="session1")
        session2 = Session(user_id="user2", session_id="session2")
        
        sessions = Session.get_all_local_sessions()
        assert len(sessions) == 2
        assert ("user1", "session1") in sessions
        assert ("user2", "session2") in sessions

    def test_clear_all_local_sessions(self):
        """Test clearing all local sessions."""
        # Create sessions
        session1 = Session(user_id="user1", session_id="session1")
        session2 = Session(user_id="user2", session_id="session2")
        
        # Verify sessions exist
        sessions = Session.get_all_local_sessions()
        assert len(sessions) == 2
        
        # Clear all sessions
        Session.clear_all_local_sessions()
        
        # Verify all sessions are cleared
        sessions = Session.get_all_local_sessions()
        assert sessions == []

    def test_local_history_trimming(self):
        """Test that local history is trimmed when it exceeds MAX_LOCAL_HISTORY."""
        session = Session(user_id="user1", session_id="session1")
        
        # Create messages exceeding the limit
        messages = []
        for i in range(SessionConfig.MAX_LOCAL_HISTORY + 10):
            messages.append(Message(
                role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i}"
            ))
        
        # Add all messages at once (should trigger trimming)
        asyncio.run(session.add_messages(messages))
        
        # Verify only MAX_LOCAL_HISTORY messages remain
        session_key = session._get_session_key()
        stored_messages = Session._local_messages[session_key]
        assert len(stored_messages) == SessionConfig.MAX_LOCAL_HISTORY
        
        # Verify the last messages are kept
        assert stored_messages[-1].content == f"Message {SessionConfig.MAX_LOCAL_HISTORY + 9}"

    def test_is_tool_message(self):
        """Test tool message detection."""
        session = Session()
        
        # Regular message
        assert not session._is_tool_message(self.test_message_1)
        
        # Tool message
        assert session._is_tool_message(self.test_tool_message)

    def test_str_and_repr(self):
        """Test string representations of Session."""
        session = Session(user_id="test_user", session_id="test_session")
        
        # Test __str__
        str_repr = str(session)
        assert str_repr == "Session(user_id='test_user', session_id='test_session')"
        
        # Test __repr__ with local backend
        repr_str = repr(session)
        assert repr_str == "Session(user_id='test_user', session_id='test_session', backend='local')"
        
        # Test __repr__ with database backend
        mock_db = MagicMock(spec=MessageDB)
        session_db = Session(
            user_id="test_user",
            session_id="test_session", 
            message_db=mock_db
        )
        repr_str_db = repr(session_db)
        assert repr_str_db == "Session(user_id='test_user', session_id='test_session', backend='database')"

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """Test error handling in various methods."""
        # Test with mock database that raises exceptions
        mock_db = AsyncMock(spec=MessageDB)
        mock_db.add_messages.side_effect = Exception("Database error")
        mock_db.get_messages.side_effect = Exception("Database error")
        mock_db.clear_history.side_effect = Exception("Database error")
        mock_db.pop_message.side_effect = Exception("Database error")
        
        session = Session(
            user_id="user1",
            session_id="session1",
            message_db=mock_db
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
