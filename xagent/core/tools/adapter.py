import json
import logging
import uuid
from typing import TYPE_CHECKING, List, Optional, Union

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import AgentConfig
from ...utils.tool_decorator import function_tool

if TYPE_CHECKING:
    from ..agent import Agent

logger = logging.getLogger(__name__)

# Module-level HTTP client pool
_http_clients: dict[str, httpx.AsyncClient] = {}


def _get_http_client(server: str) -> httpx.AsyncClient:
    """Reuse long-lived HTTP clients for sub-agent tools."""
    base_url = server.rstrip("/")
    client = _http_clients.get(base_url)
    if client is None:
        client = httpx.AsyncClient(base_url=base_url, timeout=AgentConfig.HTTP_TIMEOUT)
        _http_clients[base_url] = client
    return client


def _build_tool_user_message(input_text: str, expected_output: str) -> str:
    """Build the user message string shared by agent-as-tool and http-agent-as-tool."""
    user_message = f"### User Input:\n{input_text}"
    if expected_output:
        user_message += f"\n\n### Expected Output:\n{expected_output}"
    return user_message


_TOOL_PARAM_DESCRIPTIONS = {
    "input": "A clear, focused instruction or question for the agent, sufficient to complete the task independently, with any necessary resources included.",
    "expected_output": "Specification of the desired output format, structure, or content type.",
    "image_source": "Optional list of image URLs, file paths, or base64 strings to be included in the message. If provided, these images will be used as context for the agent's response.",
}


def agent_as_tool(
    agent: "Agent",
    name: Optional[str] = None,
    description: Optional[str] = None,
):
    """Convert an Agent instance into an OpenAI tool function."""

    @function_tool(
        name=name or agent.name,
        description=description or agent.description,
        param_descriptions=_TOOL_PARAM_DESCRIPTIONS,
    )
    async def tool_func(input: str, expected_output: str, image_source: Optional[List[str]] = None):
        user_message = _build_tool_user_message(input, expected_output)
        return await agent.chat(
            user_message=user_message,
            image_source=image_source,
            user_id=f"agent_{agent.name}_as_tool",
            session_id=f"{uuid.uuid4()}",
        )

    return tool_func


def http_agent_as_tool(
    server: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
):
    """Convert an HTTP-based agent into an OpenAI tool function."""

    @function_tool(
        name=name,
        description=description,
        param_descriptions=_TOOL_PARAM_DESCRIPTIONS,
    )
    async def tool_func(input: str, expected_output: str, image_source: Optional[List[str]] = None):
        user_message = _build_tool_user_message(input, expected_output)

        payload = {
            "user_id": f"http_agent_tool_{name or 'default'}_{uuid.uuid4().hex[:8]}",
            "session_id": f"session_{uuid.uuid4().hex[:8]}",
            "user_message": user_message,
            "stream": False,
        }

        if image_source:
            payload["image_source"] = image_source

        @retry(
            stop=stop_after_attempt(AgentConfig.RETRY_ATTEMPTS),
            wait=wait_exponential(multiplier=1, min=AgentConfig.RETRY_MIN_WAIT, max=AgentConfig.RETRY_MAX_WAIT),
        )
        async def make_http_request():
            client = _get_http_client(server)
            return await client.post("/chat", json=payload)

        try:
            response = await make_http_request()

            if response.status_code == 200:
                data = response.json()
                reply = data.get("reply", "")
                if reply:
                    return reply
                return "Empty reply from HTTP Agent"

            if response.status_code == 500:
                try:
                    error_detail = response.json().get("detail", "Internal server error")
                    return f"HTTP Agent internal error: {error_detail}"
                except Exception:
                    return f"HTTP Agent internal error: {response.text[:AgentConfig.ERROR_RESPONSE_PREVIEW_LENGTH]}"

            return f"HTTP Agent error {response.status_code}: {response.text[:AgentConfig.ERROR_RESPONSE_PREVIEW_LENGTH]}"

        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as e:
            error_type = type(e).__name__.replace('Exception', '').replace('Error', '')
            return f"HTTP Agent {error_type.lower()}: {str(e)}"
        except json.JSONDecodeError as e:
            return f"Invalid JSON response: {str(e)}"
        except Exception as e:
            logger.exception("HTTP Agent call failed: %s", e)
            return f"Unexpected error: {str(e)}"

    return tool_func


def convert_sub_agents(
    sub_agents: Optional[List[Union[tuple[str, str, str], "Agent"]]],
) -> Optional[list]:
    """Convert a list of sub-agents (Agent instances or HTTP tuples) into tool functions."""
    # Import here to avoid circular import at module level
    from ..agent import Agent

    tools = []
    for item in sub_agents or []:
        if isinstance(item, tuple) and len(item) == 3:
            name, description, server = item
            tools.append(http_agent_as_tool(server=server, name=name, description=description))
        elif isinstance(item, Agent):
            tools.append(agent_as_tool(item))
        else:
            logger.warning(
                "Invalid sub_agent type: %s. Must be tuple[name, description, server_url] or Agent instance.",
                type(item),
            )
    return tools if tools else None
