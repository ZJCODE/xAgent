import asyncio
import unittest

from xagent.core.agent import Agent
from xagent.core.config import AgentConfig, ReplyType
from xagent.core.handlers.message import MessageHandler
from xagent.core.inbox import INBOX_KIND_METADATA_KEY, InboxItem, InboxKind
from xagent.core.runtime import ScheduledDeliveryContext, scheduled_delivery_context
from xagent.integrations.langfuse import NoopObservabilityRuntime
from xagent.schemas import MessageType, RoleType

from tests.test_agent_chat_flow import (
    CapturingModelClient,
    FakeMemoryHandler,
    FakeToolExecutor,
    FakeToolManager,
    InMemoryMessageStorage,
)


class AgentInboxTests(unittest.IsolatedAsyncioTestCase):
    def _build_agent(self, storage, model_client, memory_handler=None):
        agent = Agent.__new__(Agent)
        agent.model = AgentConfig.DEFAULT_MODEL
        agent.system_prompt = ""
        agent._assistant_sender_id = "agent"
        agent.supports_vision = True
        agent.max_history = AgentConfig.DEFAULT_MAX_HISTORY
        agent.max_iter = AgentConfig.DEFAULT_MAX_ITER
        agent.max_concurrent_tools = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS
        agent.observability = NoopObservabilityRuntime()
        agent.tool_manager = FakeToolManager()
        agent.model_client = model_client
        agent.message_storage = storage
        agent.message_handler = MessageHandler(message_storage=storage, system_prompt="")
        agent.memory_handler = memory_handler or FakeMemoryHandler()
        agent.tool_executor = FakeToolExecutor()
        return agent

    async def test_user_turn_stores_inbox_kind_metadata(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([(ReplyType.SIMPLE_REPLY, "ok")])
        agent = self._build_agent(storage, model_client)

        events = [
            event
            async for event in agent.chat_events(
                user_message="hello",
                user_id="alice",
                channel="cli",
            )
        ]

        self.assertTrue(any(event.get("type") == "done" for event in events))
        user_msg = storage.messages[0]
        self.assertEqual(user_msg.role, RoleType.USER)
        self.assertEqual(user_msg.metadata.get(INBOX_KIND_METADATA_KEY), InboxKind.USER_TURN.value)
        self.assertEqual(len(model_client.calls), 1)

    async def test_scheduled_turn_kind_is_not_a_human_utterance(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([(ReplyType.SIMPLE_REPLY, "done")])
        agent = self._build_agent(storage, model_client)

        events = [
            event
            async for event in agent.chat_events(
                user_message=AgentConfig.scheduled_agent_prompt("ping the room"),
                user_id="web_user",
                channel="api",
                inbox_kind=InboxKind.SCHEDULED_TURN,
            )
        ]

        self.assertTrue(any(event.get("type") == "done" for event in events))
        user_msg = storage.messages[0]
        self.assertEqual(user_msg.metadata.get(INBOX_KIND_METADATA_KEY), InboxKind.SCHEDULED_TURN.value)
        self.assertEqual(user_msg.metadata.get("source"), "scheduled_task")
        self.assertEqual(len(model_client.calls), 1)

    async def test_delivery_context_upgrades_user_turn_to_scheduled_turn(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([(ReplyType.SIMPLE_REPLY, "done")])
        agent = self._build_agent(storage, model_client)
        context = ScheduledDeliveryContext(
            channel="api",
            user_id="web_user",
            target={"user_id": "web_user"},
            metadata={"source": "scheduled_task", "task_id": "task-1"},
        )

        with scheduled_delivery_context(context):
            events = [
                event
                async for event in agent.chat_events(
                    user_message="This scheduled task is now due.",
                    user_id="web_user",
                )
            ]

        self.assertTrue(any(event.get("type") == "done" for event in events))
        user_msg = storage.messages[0]
        self.assertEqual(user_msg.channel, "api")
        self.assertEqual(user_msg.metadata.get("source"), "scheduled_task")
        self.assertEqual(user_msg.metadata.get(INBOX_KIND_METADATA_KEY), InboxKind.SCHEDULED_TURN.value)

    async def test_observe_does_not_call_the_model(self):
        storage = InMemoryMessageStorage()
        memory_handler = FakeMemoryHandler()
        model_client = CapturingModelClient([])
        agent = self._build_agent(storage, model_client, memory_handler=memory_handler)

        result = await agent.submit(
            InboxItem(
                kind=InboxKind.OBSERVATION,
                content="door sensor tripped",
                user_id="alice",
                channel="api",
                metadata={"source": "camera", "event_type": "presence"},
            )
        )

        self.assertFalse(result.replied)
        self.assertEqual(len(storage.messages), 1)
        self.assertEqual(storage.messages[0].type, MessageType.CONTEXT_EVENT)
        self.assertEqual(storage.messages[0].channel, "api")
        self.assertEqual(storage.messages[0].metadata["source"], "camera")
        self.assertEqual(storage.messages[0].sender_id, "alice")
        self.assertEqual(
            storage.messages[0].metadata.get(INBOX_KIND_METADATA_KEY),
            InboxKind.OBSERVATION.value,
        )
        self.assertEqual(model_client.calls, [])
        self.assertFalse(agent.inbox.busy)

    async def test_scheduled_turn_prompt_does_not_treat_task_as_speech(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([(ReplyType.SIMPLE_REPLY, "done")])
        agent = self._build_agent(storage, model_client)

        events = [
            event
            async for event in agent.chat_events(
                user_message=AgentConfig.scheduled_agent_prompt("ping the room"),
                user_id="web_user",
                channel="api",
                inbox_kind=InboxKind.SCHEDULED_TURN,
            )
        ]

        self.assertTrue(any(event.get("type") == "done" for event in events))
        rendered = "\n".join(
            str(message.get("content") or "")
            for message in model_client.calls[0]
        )
        self.assertIn("[scheduled task]", rendered)
        self.assertIn("[for=web_user]", rendered)
        self.assertNotIn("[speaker=web_user]", rendered)
        self.assertIn("due scheduled task", rendered)
        self.assertNotIn("what web_user just said", rendered)

    async def test_overlapping_chat_events_run_serially(self):
        storage = InMemoryMessageStorage()
        started = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        class SerialModelClient(CapturingModelClient):
            def __init__(self):
                super().__init__(responses=[])
                self.active = 0
                self.max_active = 0

            async def model_turn_events(self, messages, tool_specs, instructions=None, stream=False):
                self.calls.append(messages)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                call_index = len(self.calls)
                order.append(f"start:{call_index}")
                if call_index == 1:
                    started.set()
                    await release.wait()
                self.active -= 1
                order.append(f"end:{call_index}")
                yield type(
                    "Event",
                    (),
                    {"type": "delta", "delta": "ok", "error": None, "tool_calls": None},
                )()

        model_client = SerialModelClient()
        agent = self._build_agent(storage, model_client)

        async def run_turn(text: str):
            return [
                event
                async for event in agent.chat_events(
                    user_message=text,
                    user_id="alice",
                    channel="cli",
                )
            ]

        first = asyncio.create_task(run_turn("first"))
        await started.wait()
        second = asyncio.create_task(run_turn("second"))
        await asyncio.sleep(0)
        self.assertTrue(agent.inbox.busy)
        self.assertEqual(model_client.max_active, 1)
        release.set()
        await asyncio.gather(first, second)

        self.assertEqual(model_client.max_active, 1)
        self.assertEqual(len(model_client.calls), 2)
        self.assertEqual(order, ["start:1", "end:1", "start:2", "end:2"])
        kinds = [
            message.metadata.get(INBOX_KIND_METADATA_KEY)
            for message in storage.messages
            if message.role == RoleType.USER
        ]
        self.assertEqual(kinds, [InboxKind.USER_TURN.value, InboxKind.USER_TURN.value])
