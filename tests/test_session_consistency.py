import asyncio
import contextlib
import io
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from xagent.components.message import MessageStorageLocal
from xagent.core.agent import Agent
from xagent.interfaces.cli import AgentCLI
from xagent.interfaces.server import AgentHTTPServer


class DummyResponsesAPI:
    def __init__(self, create_responses):
        self._create_responses = list(create_responses)

    async def create(self, **kwargs):
        if not self._create_responses:
            raise AssertionError("No fake create response configured")
        response = self._create_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def parse(self, **kwargs):
        raise AssertionError("Structured output is not expected in this test")


class DummyMemoryStorage:
    def __init__(self):
        self.llm_service = SimpleNamespace(
            should_preprocess_query=lambda query, pre_chat=None: False
        )

    async def add(self, user_id, messages):
        return None

    async def store(self, user_id, content):
        return ""

    async def retrieve(self, user_id, query, limit=5, query_context=None, enable_query_process=False):
        return []

    async def extract_meta(self, user_id, days=1):
        return []

    async def clear(self, user_id):
        return None

    async def delete(self, memory_ids):
        return None


class SessionConsistencyTests(unittest.TestCase):
    def test_cli_and_http_clear_use_normalized_session_ids(self):
        with TemporaryDirectory() as temp_dir:
            storage = MessageStorageLocal(path=f"{temp_dir}/messages.sqlite3")
            client = SimpleNamespace(
                responses=DummyResponsesAPI(
                    [
                        SimpleNamespace(output_text="first", output=[]),
                        SimpleNamespace(output_text="second", output=[]),
                    ]
                )
            )
            agent = Agent(
                name="Test Agent",
                client=client,
                message_storage=storage,
                memory_storage=DummyMemoryStorage(),
            )

            server = AgentHTTPServer(agent=agent, enable_web=False)
            http_client = TestClient(server.app)

            first_response = http_client.post(
                "/chat",
                json={
                    "user_id": "user-1",
                    "session_id": "session-a",
                    "user_message": "hello",
                },
            )
            self.assertEqual(first_response.status_code, 200)
            normalized_session_id = agent.normalize_session_id("session-a")
            messages = asyncio.run(
                storage.get_messages("user-1", normalized_session_id, count=10)
            )
            self.assertEqual([message.content for message in messages], ["hello", "first"])

            cli = AgentCLI.__new__(AgentCLI)
            cli.agent = agent
            cli.message_storage = storage
            cli.config_path = None
            cli.verbose = False

            with patch("builtins.input", side_effect=["clear", "bye"]):
                with contextlib.redirect_stdout(io.StringIO()):
                    asyncio.run(
                        cli.chat_interactive(
                            user_id="user-1",
                            session_id="session-a",
                            stream=False,
                        )
                    )

            cleared_messages = asyncio.run(
                storage.get_messages("user-1", normalized_session_id, count=10)
            )
            self.assertEqual(cleared_messages, [])

            second_response = http_client.post(
                "/chat",
                json={
                    "user_id": "user-1",
                    "session_id": "session-a",
                    "user_message": "hello again",
                },
            )
            self.assertEqual(second_response.status_code, 200)
            recreated_messages = asyncio.run(
                storage.get_messages("user-1", normalized_session_id, count=10)
            )
            self.assertEqual(
                [message.content for message in recreated_messages],
                ["hello again", "second"],
            )

            clear_response = http_client.post(
                "/clear_session",
                json={"user_id": "user-1", "session_id": "session-a"},
            )
            self.assertEqual(clear_response.status_code, 200)
            final_messages = asyncio.run(
                storage.get_messages("user-1", normalized_session_id, count=10)
            )
            self.assertEqual(final_messages, [])
