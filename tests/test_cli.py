"""
Test xAgent CLI functionality.
"""

import pytest
import asyncio
from unittest.mock import patch, AsyncMock
from xagent.core.cli import CLIAgent


class TestCLIAgent:
    """Test cases for CLIAgent."""
    
    @pytest.fixture
    def mock_config(self):
        """Mock configuration data."""
        return {
            "agent": {
                "name": "TestAgent",
                "system_prompt": "You are a test agent.",
                "model": "gpt-4.1-mini",
                "tools": [],
                "mcp_servers": [],
                "use_local_session": True
            }
        }
    
    @pytest.fixture
    def cli_agent(self, mock_config):
        """Create CLI agent with mocked config."""
        with patch.object(CLIAgent, '_load_config', return_value=mock_config):
            with patch.object(CLIAgent, '_initialize_agent') as mock_agent_init:
                with patch.object(CLIAgent, '_initialize_message_db', return_value=None):
                    # Create a mock agent that can be awaited
                    mock_agent = AsyncMock()
                    mock_agent.name = "TestAgent"
                    mock_agent.model = "gpt-4.1-mini"
                    mock_agent.tools = {}
                    mock_agent.mcp_tools = {}
                    mock_agent_init.return_value = mock_agent
                    return CLIAgent(config_path="test_config.yaml")
    
    def test_cli_agent_initialization(self, cli_agent):
        """Test CLI agent initialization."""
        assert cli_agent.toolkit_path == "toolkit"
        assert cli_agent.agent.name == "TestAgent"
        assert cli_agent.agent.model == "gpt-4.1-mini"
    
    @pytest.mark.asyncio
    async def test_chat_single(self, cli_agent):
        """Test single message chat."""
        # Mock the agent to return a test response
        cli_agent.agent.return_value = "Test response"
        
        response = await cli_agent.chat_single("Test message")
        
        assert response == "Test response"
        cli_agent.agent.assert_called_once()
    
    def test_show_help(self, cli_agent, capsys):
        """Test help display."""
        cli_agent._show_help()
        captured = capsys.readouterr()
        assert "Available commands:" in captured.out
        assert "Available tools:" in captured.out


def test_main_function():
    """Test main function argument parsing."""
    # Test that main function exists and can be imported
    from xagent.core.cli import main
    assert callable(main)


if __name__ == "__main__":
    pytest.main([__file__])
