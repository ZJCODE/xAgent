import asyncio
import unittest

from xagent.core.agent import Agent
from xagent.core.config import AgentConfig, ReplyType
from xagent.core.handlers.message import MessageHandler
from xagent.core.inbox import (
    INBOX_KIND_METADATA_KEY,
    SCHEDULED_AGENT_PROMPT_PREFIX,
    TASK_CONTENT_METADATA_KEY,
    AgentInbox,
    InboxItem,
    InboxKind,
    scheduled_task_display_content,
)
from xagent.core.delivery import ImmediateDeliverySession, RejectingDeliverySession
from xagent.core.runtime import ScheduledDeliveryContext, scheduled_delivery_context
from xagent.integrations.langfuse import NoopObservabilityRuntime
from xagent.schemas import Message, MessageType, RoleType
from xagent.interfaces.server.serializers import message_item

from tests.test_agent_chat_flow import (
    CapturingModelClient,
    FakeMemoryHandler,
    FakeToolExecutor,
    FakeToolManager,
    InMemoryMessageStorage,
)


class ScheduledTaskDisplayContentTests(unittest.TestCase):
    def test_prefers_task_content_metadata(self):
        wrapped = AgentConfig.scheduled_agent_prompt("ping the room")
        self.assertEqual(
            scheduled_task_display_content(
                wrapped,
                {TASK_CONTENT_METADATA_KEY: "ping the room"},
            ),
            "ping the room",
        )

    def test_strips_legacy_wrapper_prefix(self):
        wrapped = SCHEDULED_AGENT_PROMPT_PREFIX + "看下 CPU"
        self.assertEqual(scheduled_task_display_content(wrapped), "看下 CPU")
        self.assertEqual(scheduled_task_display_content("plain task"), "plain task")

    def test_frontend_prefix_stays_in_sync(self):
        from pathlib import Path

        source = Path("/workspace/frontend/src/lib/scheduledMessage.ts").read_text(encoding="utf-8")
        self.assertIn(SCHEDULED_AGENT_PROMPT_PREFIX.replace("\n", "\\n"), source)

    def test_message_metadata_records_task_body(self):
        wrapped = InboxItem(
            kind=InboxKind.SCHEDULED_TURN,
            content=AgentConfig.scheduled_agent_prompt("ping the room"),
            user_id="web_user",
        ).message_metadata()
        unwrapped = InboxItem(
            kind=InboxKind.SCHEDULED_TURN,
            content="ping the room",
            user_id="web_user",
        ).message_metadata()
        self.assertEqual(wrapped[TASK_CONTENT_METADATA_KEY], "ping the room")
        self.assertEqual(unwrapped[TASK_CONTENT_METADATA_KEY], "ping the room")

    def test_message_item_exposes_inbox_kind_and_task_content(self):
        message = Message.create("ping the room", role=RoleType.USER, sender_id="web_user")
        message.channel = "api"
        message.metadata.update(
            InboxItem(
                kind=InboxKind.SCHEDULED_TURN,
                content="ping the room",
                user_id="web_user",
                channel="api",
            ).message_metadata()
        )
        item = message_item(message)
        self.assertEqual(item["role"], "user")
        self.assertEqual(item["content"], "ping the room")
        self.assertEqual(item["metadata"][INBOX_KIND_METADATA_KEY], "scheduled_turn")
        self.assertEqual(item["metadata"][TASK_CONTENT_METADATA_KEY], "ping the room")


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
                user_message="ping the room",
                user_id="web_user",
                channel="api",
                inbox_kind=InboxKind.SCHEDULED_TURN,
            )
        ]

        self.assertTrue(any(event.get("type") == "done" for event in events))
        user_msg = storage.messages[0]
        self.assertEqual(user_msg.content, "ping the room")
        self.assertEqual(user_msg.metadata.get(INBOX_KIND_METADATA_KEY), InboxKind.SCHEDULED_TURN.value)
        self.assertEqual(user_msg.metadata.get("source"), "scheduled_task")
        self.assertEqual(user_msg.metadata.get(TASK_CONTENT_METADATA_KEY), "ping the room")
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
                user_message="ping the room",
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
        self.assertNotIn("This scheduled task is now due", rendered)

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

    async def test_rejecting_delivery_does_not_persist_assistant(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([(ReplyType.SIMPLE_REPLY, "secret")])
        agent = self._build_agent(storage, model_client)
        session = RejectingDeliverySession()

        events = [
            event
            async for event in agent.chat_events(
                user_message="hello",
                user_id="alice",
                channel="cli",
                session=session,
            )
        ]

        self.assertTrue(any(event.get("type") == "message_done" for event in events))
        assistant = [message for message in storage.messages if message.role == RoleType.ASSISTANT]
        self.assertEqual(assistant, [])

    async def test_initiative_does_not_store_user_message(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, '{"admit": true}'),
            (ReplyType.SIMPLE_REPLY, "checking in"),
        ])
        agent = self._build_agent(storage, model_client)
        item = InboxItem(
            kind=InboxKind.INITIATIVE_TURN,
            content="say hello",
            user_id="alice",
            channel="cli",
        )
        events = [
            event
            async for event in agent.submit(item, session=ImmediateDeliverySession())
        ]
        self.assertTrue(any(event.get("type") == "done" for event in events))
        user_msgs = [message for message in storage.messages if message.role == RoleType.USER]
        assistant = [message for message in storage.messages if message.role == RoleType.ASSISTANT]
        self.assertEqual(user_msgs, [])
        self.assertEqual([message.content for message in assistant], ["checking in"])


class InitiativeQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_human_overtakes_initiative(self):
        inbox = AgentInbox()
        blocker = InboxItem(kind=InboxKind.USER_TURN, content="busy", user_id="u")
        initiative = InboxItem(kind=InboxKind.INITIATIVE_TURN, content="nudge", user_id="u")
        human = InboxItem(kind=InboxKind.USER_TURN, content="hello", user_id="u")
        await inbox.acquire_turn(blocker)
        initiative_task = asyncio.create_task(inbox.acquire_turn(initiative))
        await asyncio.sleep(0)
        human_task = asyncio.create_task(inbox.acquire_turn(human))
        await asyncio.sleep(0)
        await inbox.release_turn()
        human_lease = await human_task
        self.assertEqual(human_lease.item.kind, InboxKind.USER_TURN)
        await inbox.release_turn()
        initiative_lease = await initiative_task
        self.assertEqual(initiative_lease.item.kind, InboxKind.INITIATIVE_TURN)
        await inbox.release_turn()

    async def test_started_initiative_is_not_preempted(self):
        inbox = AgentInbox()
        initiative = InboxItem(kind=InboxKind.INITIATIVE_TURN, content="nudge", user_id="u")
        human = InboxItem(kind=InboxKind.USER_TURN, content="hello", user_id="u")
        lease = await inbox.acquire_turn(initiative)
        self.assertEqual(lease.item.kind, InboxKind.INITIATIVE_TURN)
        human_task = asyncio.create_task(inbox.acquire_turn(human))
        await asyncio.sleep(0.01)
        self.assertTrue(inbox.busy)
        self.assertEqual(inbox._lease.item.kind, InboxKind.INITIATIVE_TURN)
        self.assertFalse(human_task.done())
        await inbox.release_turn()
        human_lease = await human_task
        self.assertEqual(human_lease.item.kind, InboxKind.USER_TURN)
        await inbox.release_turn()

    async def test_second_initiative_is_dropped(self):
        inbox = AgentInbox()
        first = InboxItem(kind=InboxKind.INITIATIVE_TURN, content="one", user_id="u")
        second = InboxItem(kind=InboxKind.INITIATIVE_TURN, content="two", user_id="u")
        task = asyncio.create_task(inbox.acquire_turn(first))
        await asyncio.sleep(0)
        dropped = await inbox.acquire_turn(second)
        self.assertIsNone(dropped)
        self.assertTrue(inbox.has_initiative())
        lease = await task
        self.assertEqual(lease.item.content, "one")
        await inbox.release_turn()
        self.assertFalse(inbox.has_initiative())
