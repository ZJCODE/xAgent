import asyncio
import unittest

from xagent.core.agent import Agent
from xagent.core.config import AgentConfig, ReplyType
from xagent.core.handlers.message import MessageHandler
from xagent.core.handlers.model import ChatToolCall, ModelStreamEvent
from xagent.core.inbox import (
    INBOX_KIND_METADATA_KEY,
    SCHEDULED_AGENT_PROMPT_PREFIX,
    TASK_CONTENT_METADATA_KEY,
    AgentInbox,
    InboxItem,
    InboxKind,
    scheduled_task_display_content,
)
from xagent.core.runtime import ScheduledDeliveryContext, scheduled_delivery_context
from xagent.integrations.langfuse import NoopObservabilityRuntime
from xagent.schemas import Message, MessageType, RoleType
from xagent.interfaces.server.serializers import message_item

from tests.test_agent_chat_flow import (
    CapturingModelClient,
    FakeMemoryHandler,
    FakeToolCall,
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
        self.assertIsNone(item["sender_name"])
        self.assertEqual(item["metadata"][INBOX_KIND_METADATA_KEY], "scheduled_turn")
        self.assertEqual(item["metadata"][TASK_CONTENT_METADATA_KEY], "ping the room")

    def test_message_item_exposes_feishu_sender_name(self):
        message = Message.create("早啊", role=RoleType.USER, sender_id="ou_user")
        message.channel = "feishu"
        message.metadata = {"sender_name": "Jun"}
        item = message_item(message)
        self.assertEqual(item["sender_id"], "ou_user")
        self.assertEqual(item["sender_name"], "Jun")
        self.assertEqual(item["channel"], "feishu")


class AgentInboxTests(unittest.IsolatedAsyncioTestCase):
    def _build_agent(self, storage, model_client, memory_handler=None, tool_executor=None):
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
        agent.tool_executor = tool_executor or FakeToolExecutor()
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

    async def test_abort_is_noop_when_idle(self):
        agent = self._build_agent(InMemoryMessageStorage(), CapturingModelClient([]))
        self.assertFalse(agent.abort())
        self.assertFalse(agent.inbox.busy)

    async def test_abort_skips_tools_returned_by_the_current_model_call(self):
        storage = InMemoryMessageStorage()
        started = asyncio.Event()
        release = asyncio.Event()

        class PausingToolCallModel(CapturingModelClient):
            async def model_turn_events(self, messages, tool_specs, instructions=None, stream=False):
                self.calls.append(messages)
                started.set()
                await release.wait()
                yield ModelStreamEvent(
                    type="tool_calls",
                    tool_calls=[
                        ChatToolCall(call_id="call-1", name="lookup", arguments="{}"),
                    ],
                )

        model_client = PausingToolCallModel([(ReplyType.SIMPLE_REPLY, "unused")])
        tool_executor = FakeToolExecutor()
        agent = self._build_agent(storage, model_client, tool_executor=tool_executor)

        async def run_turn():
            return [
                event
                async for event in agent.chat_events(
                    user_message="work",
                    user_id="alice",
                    channel="cli",
                )
            ]

        task = asyncio.create_task(run_turn())
        await started.wait()
        self.assertTrue(agent.abort())
        release.set()
        events = await task
        types = [event.get("type") for event in events]
        self.assertEqual(types, ["aborted", "done"])
        self.assertEqual(tool_executor.seen_input_messages, [])
        self.assertEqual(len(model_client.calls), 1)

    async def test_abort_stops_after_the_current_tool_batch(self):
        storage = InMemoryMessageStorage()
        started = asyncio.Event()
        release = asyncio.Event()

        class PausingToolExecutor(FakeToolExecutor):
            async def handle_tool_calls(self, tool_calls, input_messages, max_concurrent_tools, **kwargs):
                started.set()
                await release.wait()
                return await super().handle_tool_calls(
                    tool_calls,
                    input_messages,
                    max_concurrent_tools,
                    **kwargs,
                )

        model_client = CapturingModelClient([
            (ReplyType.TOOL_CALL, [FakeToolCall()]),
            (ReplyType.SIMPLE_REPLY, "should not run"),
        ])
        tool_executor = PausingToolExecutor()
        agent = self._build_agent(storage, model_client, tool_executor=tool_executor)

        async def run_turn():
            return [
                event
                async for event in agent.chat_events(
                    user_message="work",
                    user_id="alice",
                    channel="cli",
                )
            ]

        task = asyncio.create_task(run_turn())
        await started.wait()
        self.assertTrue(agent.abort())
        release.set()
        events = await task
        types = [event.get("type") for event in events]
        self.assertIn("tool_call", types)
        self.assertIn("tool_result", types)
        self.assertEqual(types[-2:], ["aborted", "done"])
        self.assertEqual(len(model_client.calls), 1)
        self.assertEqual(len(model_client.responses), 1)


class AgentInboxAbortTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_abort_is_noop_when_idle(self):
        inbox = AgentInbox()
        self.assertFalse(inbox.request_abort())
        self.assertFalse(inbox.abort_requested())

    async def test_request_abort_sets_flag_while_busy(self):
        inbox = AgentInbox()
        await inbox.acquire_turn()
        try:
            self.assertTrue(inbox.request_abort())
            self.assertTrue(inbox.abort_requested())
        finally:
            inbox.release_turn()
        self.assertFalse(inbox.abort_requested())
