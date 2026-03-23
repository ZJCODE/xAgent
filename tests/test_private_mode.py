"""Tests for MessageStorageInMemory and Agent private mode."""

import unittest

from xagent.components.message.memory_messages import MessageStorageInMemory
from xagent.core.agent import Agent
from xagent.core.config import ReplyType
from xagent.core.handlers.message import MessageHandler
from xagent.schemas import Message, RoleType


# ── Reusable fakes (same pattern as test_agent_chat_flow.py) ──


class FakeToolManager:
    def __init__(self, tools=None):
        self._tools = dict(tools or {})
        self.cached_tool_specs = None

    async def ensure_mcp_ready(self):
        return None


class FakeMemoryHandler:
    def __init__(self):
        self.scheduled_messages = None

    async def get_recent_context(self):
        return ""

    def schedule_diary_write(self, messages):
        self.scheduled_messages = messages


class CapturingModelClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call(self, messages, tool_specs, instructions=None, output_type=None, stream=False, store_reply=None):
        self.calls.append(messages)
        return self.responses.pop(0)


class FakeToolExecutor:
    async def handle_tool_calls(self, tool_calls, input_messages, max_concurrent_tools):
        return None


# ── MessageStorageInMemory tests ──


class MessageStorageInMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_and_get_messages(self):
        storage = MessageStorageInMemory()
        msg = Message.create("hello", role=RoleType.USER, sender_id="alice")
        await storage.add_messages(msg)
        result = await storage.get_messages(10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "hello")

    async def test_add_list_of_messages(self):
        storage = MessageStorageInMemory()
        msgs = [
            Message.create("one", role=RoleType.USER, sender_id="alice"),
            Message.create("two", role=RoleType.ASSISTANT, sender_id="agent:test"),
        ]
        await storage.add_messages(msgs)
        result = await storage.get_messages(10)
        self.assertEqual(len(result), 2)

    async def test_get_messages_respects_count(self):
        storage = MessageStorageInMemory()
        for i in range(5):
            await storage.add_messages(
                Message.create(f"msg-{i}", role=RoleType.USER, sender_id="alice")
            )
        result = await storage.get_messages(2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].content, "msg-3")
        self.assertEqual(result[1].content, "msg-4")

    async def test_get_messages_with_offset(self):
        storage = MessageStorageInMemory()
        for i in range(5):
            await storage.add_messages(
                Message.create(f"msg-{i}", role=RoleType.USER, sender_id="alice")
            )
        result = await storage.get_messages(2, offset=1)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].content, "msg-2")
        self.assertEqual(result[1].content, "msg-3")

    async def test_clear_messages(self):
        storage = MessageStorageInMemory()
        await storage.add_messages(
            Message.create("hello", role=RoleType.USER, sender_id="alice")
        )
        await storage.clear_messages()
        self.assertEqual(await storage.get_message_count(), 0)

    async def test_pop_message(self):
        storage = MessageStorageInMemory()
        await storage.add_messages(
            Message.create("hello", role=RoleType.USER, sender_id="alice")
        )
        msg = await storage.pop_message()
        self.assertEqual(msg.content, "hello")
        self.assertEqual(await storage.get_message_count(), 0)

    async def test_pop_empty_returns_none(self):
        storage = MessageStorageInMemory()
        self.assertIsNone(await storage.pop_message())

    async def test_get_messages_zero_count(self):
        storage = MessageStorageInMemory()
        await storage.add_messages(
            Message.create("hello", role=RoleType.USER, sender_id="alice")
        )
        result = await storage.get_messages(0)
        self.assertEqual(result, [])


# ── Agent private mode tests ──


def _build_agent(storage, model_client, memory_handler=None, tool_executor=None, tools=None):
    agent = Agent.__new__(Agent)
    agent.output_type = None
    agent.name = "test"
    agent.system_prompt = ""
    agent._assistant_sender_id = "agent:test"
    agent._memory_tools_enabled = True
    agent._private_mode = False
    agent._private_storage = None
    agent._private_message_handler = None
    agent.tool_manager = FakeToolManager(tools=tools)
    agent.model_client = model_client
    agent.message_storage = storage
    agent.message_handler = MessageHandler(message_storage=storage, system_prompt="")
    agent.memory_handler = memory_handler or FakeMemoryHandler()
    agent.tool_executor = tool_executor or FakeToolExecutor()
    return agent


class AgentPrivateModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_mode_does_not_write_to_main_storage(self):
        """Messages in private mode should NOT appear in the main storage."""
        main_storage = MessageStorageInMemory()
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "private reply"),
        ])
        agent = _build_agent(storage=main_storage, model_client=model_client)

        result = await Agent.chat(
            agent,
            user_message="secret question",
            user_id="alice",
            history_count=10,
            max_iter=2,
            private=True,
        )

        self.assertEqual(result, "private reply")
        # Main storage should be untouched
        main_messages = await main_storage.get_messages(100)
        self.assertEqual(len(main_messages), 0)

    async def test_private_mode_accumulates_across_calls(self):
        """Multiple private calls should share the same in-memory storage."""
        main_storage = MessageStorageInMemory()
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "reply-1"),
            (ReplyType.SIMPLE_REPLY, "reply-2"),
        ])
        agent = _build_agent(storage=main_storage, model_client=model_client)

        await Agent.chat(agent, user_message="msg-1", user_id="alice", private=True)
        await Agent.chat(agent, user_message="msg-2", user_id="alice", private=True)

        # Private storage should have accumulated messages
        self.assertTrue(agent._private_mode)
        private_msgs = await agent._private_storage.get_messages(100)
        # 2 user + 2 assistant = 4
        self.assertEqual(len(private_msgs), 4)
        # Main storage untouched
        self.assertEqual(len(await main_storage.get_messages(100)), 0)

    async def test_switching_from_private_to_normal_discards_private(self):
        """Switching back to normal mode should discard private messages."""
        main_storage = MessageStorageInMemory()
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "private reply"),
            (ReplyType.SIMPLE_REPLY, "normal reply"),
        ])
        agent = _build_agent(storage=main_storage, model_client=model_client)

        await Agent.chat(agent, user_message="secret", user_id="alice", private=True)
        self.assertTrue(agent._private_mode)

        await Agent.chat(agent, user_message="hello", user_id="alice", private=False)
        self.assertFalse(agent._private_mode)
        self.assertIsNone(agent._private_storage)
        self.assertIsNone(agent._private_message_handler)

        # Normal message should be in main storage
        main_msgs = await main_storage.get_messages(100)
        self.assertEqual(len(main_msgs), 2)  # user + assistant

    async def test_private_mode_suppresses_diary_write(self):
        """Private mode should not schedule diary writes."""
        main_storage = MessageStorageInMemory()
        memory_handler = FakeMemoryHandler()
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "reply"),
        ])
        agent = _build_agent(
            storage=main_storage,
            model_client=model_client,
            memory_handler=memory_handler,
        )

        await Agent.chat(
            agent,
            user_message="private chat",
            user_id="alice",
            private=True,
            enable_memory=True,
        )

        # schedule_diary_write should NOT have been called (scheduled_messages stays None)
        self.assertIsNone(memory_handler.scheduled_messages)

    async def test_private_mode_still_reads_memory_context(self):
        """Private mode with enable_memory=True should still inject memory context."""

        class TrackingMemoryHandler(FakeMemoryHandler):
            def __init__(self):
                super().__init__()
                self.get_recent_context_called = False

            async def get_recent_context(self):
                self.get_recent_context_called = True
                return "some diary context"

        main_storage = MessageStorageInMemory()
        memory_handler = TrackingMemoryHandler()
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "reply"),
        ])
        agent = _build_agent(
            storage=main_storage,
            model_client=model_client,
            memory_handler=memory_handler,
        )

        await Agent.chat(
            agent,
            user_message="what did we discuss yesterday?",
            user_id="alice",
            private=True,
            enable_memory=True,
        )

        self.assertTrue(memory_handler.get_recent_context_called)

    async def test_private_with_memory_disabled_skips_memory_read(self):
        """private=True + enable_memory=False should also skip memory reads."""

        class TrackingMemoryHandler(FakeMemoryHandler):
            def __init__(self):
                super().__init__()
                self.get_recent_context_called = False

            async def get_recent_context(self):
                self.get_recent_context_called = True
                return "context"

        main_storage = MessageStorageInMemory()
        memory_handler = TrackingMemoryHandler()
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "reply"),
        ])
        agent = _build_agent(
            storage=main_storage,
            model_client=model_client,
            memory_handler=memory_handler,
        )

        await Agent.chat(
            agent,
            user_message="hi",
            user_id="alice",
            private=True,
            enable_memory=False,
        )

        self.assertFalse(memory_handler.get_recent_context_called)

    async def test_private_mode_filters_write_tools_keeps_search(self):
        """Private mode should remove write memory tools but keep search_memory."""
        main_storage = MessageStorageInMemory()
        tools = {
            "write_daily_memory": lambda: None,
            "search_memory": lambda: None,
            "generate_memory_summary": lambda: None,
            "custom_tool": lambda: None,
        }
        tool_specs = [
            {"name": "write_daily_memory"},
            {"name": "search_memory"},
            {"name": "generate_memory_summary"},
            {"name": "custom_tool"},
        ]

        class SpecCapturingModelClient(CapturingModelClient):
            def __init__(self, responses):
                super().__init__(responses)
                self.received_tool_specs = None

            async def call(self, messages, tool_specs, **kwargs):
                self.received_tool_specs = tool_specs
                return self.responses.pop(0)

        model_client = SpecCapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "reply"),
        ])
        agent = _build_agent(
            storage=main_storage,
            model_client=model_client,
            tools=tools,
        )
        agent.tool_manager.cached_tool_specs = tool_specs

        await Agent.chat(
            agent,
            user_message="hi",
            user_id="alice",
            private=True,
            enable_memory=True,
        )

        spec_names = {s["name"] for s in model_client.received_tool_specs}
        self.assertIn("search_memory", spec_names)
        self.assertIn("custom_tool", spec_names)
        self.assertNotIn("write_daily_memory", spec_names)
        self.assertNotIn("generate_memory_summary", spec_names)


if __name__ == "__main__":
    unittest.main()
