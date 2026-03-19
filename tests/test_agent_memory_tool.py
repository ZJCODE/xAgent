import asyncio
import unittest
from unittest.mock import AsyncMock

from xagent.core.agent import Agent
from xagent.core.config import ReplyType


class _FakeMessageStorage:
    def __init__(self):
        self.messages = []

    async def add_messages(self, messages):
        if isinstance(messages, list):
            self.messages.extend(messages)
            return
        self.messages.append(messages)

    async def get_messages(self, history_count: int):
        return list(self.messages[-history_count:])


class _FakeMemoryStorage:
    def __init__(self):
        self.retrieve_calls = []
        self.add_calls = []

    async def retrieve(self, memory_key: str, query: str = "", limit: int = 5, journal_date=None):
        self.retrieve_calls.append(
            {
                "memory_key": memory_key,
                "query": query,
                "limit": limit,
                "journal_date": journal_date,
            }
        )
        return []

    async def add(self, memory_key: str, messages: list[dict]):
        self.add_calls.append({"memory_key": memory_key, "messages": list(messages)})


class AgentMemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_uses_recent_memory_context_without_keyword_search(self):
        message_storage = _FakeMessageStorage()
        memory_storage = _FakeMemoryStorage()
        agent = Agent(
            name="Memory Agent",
            client=object(),
            message_storage=message_storage,
            memory_storage=memory_storage,
        )
        agent.model_client.call = AsyncMock(return_value=(ReplyType.SIMPLE_REPLY, "ok"))

        reply = await agent.chat(
            user_message="帮我总结一下路线图",
            user_id="alice",
            enable_memory=True,
        )
        await asyncio.sleep(0)

        self.assertEqual(reply, "ok")
        self.assertEqual(len(memory_storage.retrieve_calls), 2)
        self.assertTrue(all(call["query"] == "" for call in memory_storage.retrieve_calls))
        self.assertIn("search_journal_memory", agent.tools)

    async def test_agent_hides_memory_tool_when_memory_disabled(self):
        message_storage = _FakeMessageStorage()
        memory_storage = _FakeMemoryStorage()
        agent = Agent(
            name="Memory Agent",
            client=object(),
            message_storage=message_storage,
            memory_storage=memory_storage,
        )
        agent.model_client.call = AsyncMock(return_value=(ReplyType.SIMPLE_REPLY, "ok"))

        await agent.chat(
            user_message="今天怎么样",
            user_id="alice",
            enable_memory=False,
        )

        self.assertEqual(memory_storage.retrieve_calls, [])
        tool_specs = agent.model_client.call.await_args.kwargs["tool_specs"]
        tool_names = [spec["name"] for spec in tool_specs or []]
        self.assertNotIn("search_journal_memory", tool_names)


if __name__ == "__main__":
    unittest.main()
