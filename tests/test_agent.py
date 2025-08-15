import json
import types
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from xagent.core.agent import Agent
from xagent.schemas.message import Message


@pytest.mark.asyncio
async def test_simple_reply_and_storage():
    # Mock client that returns a simple text reply
    mock_client = AsyncMock()
    mock_client.responses.create = AsyncMock(return_value=types.SimpleNamespace(output_text="hello"))

    agent = Agent(name="test_agent", client=mock_client)

    reply = await agent.chat("Hi there", user_id="u1", session_id="s1")
    assert reply == "hello"

    messages = await agent.message_storage.get_messages("u1", "s1", 10)
    assert any(m.role == "assistant" and m.content == "hello" for m in messages)


class SimpleOut(BaseModel):
    result: str


@pytest.mark.asyncio
async def test_structured_reply():
    mock_client = AsyncMock()
    # Simulate parse returning a parsed pydantic model
    mock_client.responses.parse = AsyncMock(return_value=types.SimpleNamespace(output_parsed=SimpleOut(result="ok")))

    agent = Agent(name="test_agent_struct", client=mock_client)

    out = await agent.chat("Give structured output", user_id="u2", session_id="s2", output_type=SimpleOut)
    assert isinstance(out, SimpleOut)
    assert out.result == "ok"


@pytest.mark.asyncio
async def test_tool_call_executes_registered_tool():
    # Prepare a tool function and attach tool_spec
    async def echo(text: str = ""):
        return {"echo": text}

    echo.tool_spec = {"name": "echo"}

    # Mock the client to first ask for a function call, then return a final text reply
    first_output = types.SimpleNamespace(
        output=[types.SimpleNamespace(type="function_call", name="echo", arguments=json.dumps({"text": "hello"}), call_id="c1")]
    )
    second_output = types.SimpleNamespace(output_text="final reply")

    mock_client = AsyncMock()
    mock_client.responses.create = AsyncMock(side_effect=[first_output, second_output])

    agent = Agent(name="tool_agent", client=mock_client, tools=[echo])

    reply = await agent.chat("Call tool", user_id="u3", session_id="s3")
    assert reply == "final reply"

    # Ensure tool call messages were stored in message_storage
    messages = await agent.message_storage.get_messages("u3", "s3", 20)
    # Should contain at least one tool call output message
    assert any(m.type == "function_call_output" and "Tool `echo` result" in m.content for m in messages)


@pytest.mark.asyncio
async def test_as_tool_delegation_calls_chat():
    agent = Agent(name="delegator")

    async def fake_chat(*args, **kwargs):
        return "from_agent"

    # Patch agent.chat to avoid complex internals
    agent.chat = fake_chat

    tool = agent.as_tool(name="delegator_tool", description="delegates to agent")
    result = await tool("input text", "expected format")
    assert result == "from_agent"


@pytest.mark.asyncio
async def test_convert_http_agent_to_tool_handles_http_responses(monkeypatch):
    # Create a fake HTTP response object
    class FakeResponse:
        def __init__(self, status_code, json_data=None, text=""):
            self.status_code = status_code
            self._json = json_data or {}
            self.text = text

        def json(self):
            return self._json

    async def fake_post(url, json=None):
        # Return a successful reply
        return FakeResponse(200, {"reply": "hello from http agent"}, text="ok")

    # Create a proper async context manager mock
    class FakeAsyncClient:
        def __init__(self, timeout=None):
            self.timeout = timeout
            
        async def __aenter__(self):
            return self
            
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
            
        async def post(self, url, json=None):
            return await fake_post(url, json)

    with patch("xagent.core.agent.httpx.AsyncClient", FakeAsyncClient):
        agent = Agent(name="http_agent")
        tool = agent._convert_http_agent_to_tool(server="http://fake-server", name="remote", description="http agent")

        result = await tool("ask something", "text")
        assert "hello from http agent" in result


@pytest.mark.asyncio
async def test_register_mcp_servers_pulls_tools(monkeypatch):
    # Create a dummy tool function returned by MCPTool
    async def mcp_tool_func(x: str = ""):
        return "ok"

    mcp_tool_func.tool_spec = {"name": "mcp_fake"}

    class DummyMCPTool:
        def __init__(self, url):
            self.url = url

        async def get_openai_tools(self):
            return [mcp_tool_func]

    monkeypatch.setattr("xagent.core.agent.MCPTool", DummyMCPTool)

    agent = Agent(name="mcp_agent", mcp_servers=["http://mcp1"])
    # Trigger registration
    await agent._register_mcp_servers(agent.mcp_servers)

    assert "mcp_fake" in agent.mcp_tools
