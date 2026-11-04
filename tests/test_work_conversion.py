"""Auto-conversion: a user turn that outlives its foreground budget becomes
a work record, releases the foreground stream, and finishes in the background."""
import asyncio
import unittest
from unittest.mock import patch

from xagent.core.agent import Agent
from xagent.core.config import AgentConfig, ReplyType
from xagent.core.handlers.message import MessageHandler
from xagent.core.handlers.model import ModelStreamEvent
from xagent.core.runtime.work_store import WorkStore
from xagent.integrations.langfuse import NoopObservabilityRuntime


class InMemoryMessageStorage:
    def __init__(self):
        self.messages = []

    async def add_messages(self, messages, **kwargs):
        items = messages if isinstance(messages, list) else [messages]
        self.messages.extend(items)
        return list(items)

    async def get_messages(self, count: int = 20, offset: int = 0):
        if count <= 0:
            return []
        end = len(self.messages) - offset if offset else len(self.messages)
        start = max(0, end - count)
        return self.messages[start:end]

    async def clear_messages(self) -> None:
        self.messages.clear()

    async def pop_message(self):
        return self.messages.pop() if self.messages else None


class FakeToolManager:
    def __init__(self):
        self._tools = {}
        self.cached_tool_specs = None

    def get_tool(self, name):
        return self._tools.get(name)


class FakeMemoryHandler:
    async def get_recent_context(self):
        return ""

    def schedule_experience_write(self, messages):
        pass


class FakeToolExecutor:
    pass


class CapturingModelClient:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    async def model_turn_events(self, messages, tool_specs, instructions=None, stream=False):
        self.calls.append(messages)
        if self._reply == ReplyType.SIMPLE_REPLY:
            yield ModelStreamEvent(type="delta", delta="the answer")
            return
        raise AssertionError(f"Unexpected reply: {self._reply}")


def build_agent(storage, model_client, work_store=None):
    agent = Agent.__new__(Agent)
    agent.model = AgentConfig.DEFAULT_MODEL
    agent.system_prompt = ""
    agent._assistant_sender_id = "agent"
    agent.supports_vision = True
    agent.recent_messages = AgentConfig.DEFAULT_RECENT_MESSAGES
    agent.max_agent_loops = AgentConfig.DEFAULT_MAX_AGENT_LOOPS
    agent.max_concurrent_tools = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS
    agent.observability = NoopObservabilityRuntime()
    agent.tool_manager = FakeToolManager()
    agent.model_client = model_client
    agent.message_storage = storage
    agent.message_handler = MessageHandler(message_storage=storage, system_prompt="")
    agent.memory_handler = FakeMemoryHandler()
    agent.tool_executor = FakeToolExecutor()
    agent.work_store = work_store
    return agent


class AutoConversionTests(unittest.IsolatedAsyncioTestCase):
    async def _finish(self, agent):
        """Wait for the background drain to finish the turn."""
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            records = await agent.turn_store.list_records()
            if records and records[0].get("state") == "completed":
                return records
            await asyncio.sleep(0.005)
        self.fail("background turn did not complete")

    async def test_user_turn_converts_to_work_and_releases_the_foreground_stream(self):
        storage = InMemoryMessageStorage()
        work_store = WorkStore()
        agent = build_agent(storage, CapturingModelClient(ReplyType.SIMPLE_REPLY), work_store)
        with patch("xagent.core.agent.AgentConfig.TURN_BACKGROUND_BUDGET_SECONDS", 0.0):
            events = [event async for event in agent.chat_events(
                user_message="帮我整理最近的日志", user_id="alice", channel="api",
                room_name="", event_id="e1",
            )]
        types = [event["type"] for event in events]
        self.assertEqual(types, ["accepted", "backgrounded", "message_done", "done"])
        backgrounded = events[1]
        self.assertTrue(backgrounded["work_id"])
        self.assertEqual(backgrounded["work_status"], "running")
        self.assertEqual(backgrounded["turn_id"], events[0]["turn_id"])
        notice = events[2]
        self.assertEqual(notice["phase"], "final")
        self.assertIn("转入后台", notice["content"])
        self.assertIn(backgrounded["work_id"], notice["content"])
        # The foreground stream ends with a done carrying the turn identity.
        self.assertEqual(events[3]["turn_id"], events[0]["turn_id"])

        records = await self._finish(agent)
        works = await work_store.list_all()
        self.assertEqual(len(works), 1)
        work = works[0]
        self.assertEqual(work["source"], "converted")
        self.assertEqual(work["status"], "completed")
        self.assertEqual(work["goal"], "帮我整理最近的日志")
        self.assertEqual(work["channel"], "api")
        self.assertEqual(work["user_id"], "alice")
        self.assertEqual(work["turn_ids"], [events[0]["turn_id"]])
        self.assertEqual(work["result_ref"], events[0]["turn_id"])
        # The turn itself finished and persisted the stamped events.
        self.assertEqual(records[0]["state"], "completed")
        self.assertEqual(records[0]["turn_id"], events[0]["turn_id"])
        persisted_types = [event["type"] for event in records[0]["events"]]
        self.assertIn("backgrounded", persisted_types)
        backgrounded_persisted = next(e for e in records[0]["events"] if e["type"] == "backgrounded")
        self.assertEqual(backgrounded_persisted["work_id"], work["id"])
        # The final answer was stored for history.
        self.assertTrue(any("the answer" in str(message.content) for message in storage.messages))

    async def test_without_a_work_runtime_the_stream_stays_foreground(self):
        storage = InMemoryMessageStorage()
        agent = build_agent(storage, CapturingModelClient(ReplyType.SIMPLE_REPLY), work_store=None)
        with patch("xagent.core.agent.AgentConfig.TURN_BACKGROUND_BUDGET_SECONDS", 0.0):
            events = [event async for event in agent.chat_events(
                user_message="hi", user_id="alice", channel="api", event_id="e2",
            )]
        self.assertEqual([event["type"] for event in events],
                         ["accepted", "backgrounded", "message_start", "message_done", "done"])
        final = next(event for event in events
                     if event.get("type") == "message_done" and event.get("phase") == "final")
        self.assertEqual(final["content"], "the answer")

    async def test_replayed_turn_is_not_released_and_creates_no_second_work(self):
        storage = InMemoryMessageStorage()
        work_store = WorkStore()
        agent = build_agent(storage, CapturingModelClient(ReplyType.SIMPLE_REPLY), work_store)
        with patch("xagent.core.agent.AgentConfig.TURN_BACKGROUND_BUDGET_SECONDS", 0.0):
            first = [event async for event in agent.chat_events(
                user_message="long task", user_id="alice", channel="api", event_id="e3",
            )]
        await self._finish(agent)
        with patch("xagent.core.agent.AgentConfig.TURN_BACKGROUND_BUDGET_SECONDS", 0.0):
            replay = [event async for event in agent.chat_events(
                user_message="long task", user_id="alice", channel="api", event_id="e3",
            )]
        # The replay replays every saved event including backgrounded, but the
        # stream is not released early and no notice is synthesized.
        self.assertTrue(any(event["type"] == "backgrounded" for event in replay))
        self.assertTrue(all(event.get("replay") for event in replay))
        self.assertFalse(any(event.get("phase") == "final" and "转入后台" in str(event.get("content"))
                             for event in replay))
        works = await work_store.list_all()
        self.assertEqual(len(works), 1)


if __name__ == "__main__":
    unittest.main()
