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

        source = (
            Path(__file__).resolve().parents[1] / "frontend/src/lib/scheduledMessage.ts"
        ).read_text(encoding="utf-8")
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

    def test_message_item_exposes_sender_name(self):
        message = Message.create("早啊", role=RoleType.USER, sender_id="wx_user")
        message.channel = "weixin"
        message.metadata = {"sender_name": "Jun"}
        item = message_item(message)
        self.assertEqual(item["sender_id"], "wx_user")
        self.assertEqual(item["sender_name"], "Jun")
        self.assertEqual(item["channel"], "weixin")


class AgentInboxTests(unittest.IsolatedAsyncioTestCase):
    def _build_agent(self, storage, model_client, memory_handler=None, tool_executor=None):
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

        result = await agent.observe(
            context="door sensor tripped",
            source="camera",
            event_type="presence",
            metadata={"source": "camera", "event_type": "presence"},
            channel="api",
            user_id="alice",
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

        async def run_turn(text: str, user_id: str):
            return [
                event
                async for event in agent.chat_events(
                    user_message=text,
                    user_id=user_id,
                    channel="cli",
                )
            ]

        first = asyncio.create_task(run_turn("first", "alice"))
        await started.wait()
        second = asyncio.create_task(run_turn("second", "bob"))
        await asyncio.sleep(0)
        self.assertTrue(agent.inbox.busy)
        self.assertEqual(model_client.max_active, 1)
        release.set()
        first_events, second_events = await asyncio.gather(first, second)

        self.assertFalse(any(
            event.get("type") == "steered" for event in [*first_events, *second_events]
        ))
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
        self.assertEqual(types, ["accepted", "aborted", "done"])
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


def _user_texts(messages) -> list[str]:
    texts = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
    return texts


def _stored_steers(storage):
    return [
        message for message in storage.messages
        if (message.metadata or {}).get("steer") is True
    ]


def _assert_tool_pairs_intact(test, messages) -> None:
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("type") != "function_call":
            continue
        nxt = messages[index + 1]
        test.assertEqual(nxt.get("type"), "function_call_output")
        test.assertEqual(nxt.get("call_id"), message.get("call_id"))


class SteerInjectionTests(unittest.IsolatedAsyncioTestCase):
    _build_agent = AgentInboxTests._build_agent
    async def test_steer_persists_across_tool_iterations(self):
        storage = InMemoryMessageStorage()
        executor = _ResponsesBatchExecutor()
        model_client = CapturingModelClient([
            (ReplyType.TOOL_CALL, [FakeToolCall(call_id="call-1")]),
            (ReplyType.TOOL_CALL, [FakeToolCall(call_id="call-2")]),
            (ReplyType.SIMPLE_REPLY, "finished"),
        ])
        agent = self._build_agent(storage, model_client, tool_executor=executor)
        executor.agent = agent

        events = [
            event
            async for event in agent.chat_events(
                user_message="inspect the service",
                user_id="alice",
                channel="cli",
                room_name="lab",
            )
        ]

        self.assertTrue(any(event.get("type") == "done" for event in events))
        self.assertEqual(len(model_client.calls), 3)
        self.assertNotIn("check the logs", _user_texts(model_client.calls[0]))
        self.assertEqual(
            _user_texts(model_client.calls[1])[-2:],
            ["check the logs", "then restart"],
        )
        self.assertEqual(_user_texts(model_client.calls[1]), _user_texts(model_client.calls[2]))
        self.assertEqual(model_client.calls[1][-4]["type"], "function_call")
        self.assertEqual(model_client.calls[1][-3]["type"], "function_call_output")
        for call in model_client.calls:
            _assert_tool_pairs_intact(self, call)
        self.assertNotIn("check the logs", repr(executor.seen[0]))

        stored = _stored_steers(storage)
        self.assertEqual([message.content for message in stored], ["check the logs", "then restart"])
        contents = [message.content for message in storage.messages]
        self.assertLess(contents.index("check the logs"), contents.index("finished"))
        self.assertLess(contents.index("then restart"), contents.index("finished"))
        self.assertTrue(all(
            message.type == MessageType.MESSAGE and message.role == RoleType.USER
            for message in stored
        ))
        self.assertFalse(any(message.type == MessageType.CONTEXT_EVENT for message in storage.messages))

    async def test_steer_during_final_reply_continues_the_loop(self):
        storage = InMemoryMessageStorage()
        agent_box = {}
        model_client = _SteerDuringReplyModel(agent_box)
        agent = self._build_agent(storage, model_client)
        agent_box["agent"] = agent

        events = [
            event
            async for event in agent.chat_events(
                user_message="summarize the numbers",
                user_id="alice",
                channel="cli",
                room_name="lab",
            )
        ]

        done = [event for event in events if event.get("type") == "message_done"]
        self.assertEqual(
            [(event.get("phase"), event.get("content")) for event in done],
            [("interim", "draft answer"), ("final", "revised answer")],
        )
        self.assertEqual(len(model_client.calls), 2)
        self.assertNotIn("use metric units", _user_texts(model_client.calls[0]))
        added = model_client.calls[1][len(model_client.calls[0]):]
        self.assertEqual(added[0], {"role": "assistant", "content": "draft answer"})
        self.assertEqual(
            [item.get("content") for item in added[1:]],
            ["use metric units", "round to two decimals"],
        )

        stored = _stored_steers(storage)
        self.assertEqual(
            [message.content for message in stored],
            ["use metric units", "round to two decimals"],
        )
        phases = [
            message.metadata.get("turn_phase")
            for message in storage.messages
            if message.role == RoleType.ASSISTANT
        ]
        self.assertEqual(phases, ["interim", "final"])
        self.assertEqual(
            [message.content for message in storage.messages],
            [
                "summarize the numbers",
                "use metric units",
                "round to two decimals",
                "draft answer",
                "revised answer",
            ],
        )
        self.assertFalse(agent.inbox.busy)
        self.assertFalse(agent.inbox.has_pending_steer())

    async def test_steer_rejects_other_user_or_room(self):
        storage = InMemoryMessageStorage()
        agent_box = {}
        model_client = _RejectingSteerModel(agent_box)
        agent = self._build_agent(storage, model_client)
        agent_box["agent"] = agent

        self.assertFalse(await agent.steer(
            "no-turn", "early", channel="cli", user_id="alice", room_name="lab",
        ))
        self.assertEqual(storage.messages, [])

        events = [
            event
            async for event in agent.chat_events(
                user_message="hello",
                user_id="alice",
                channel="cli",
                room_name="lab",
            )
        ]

        self.assertTrue(any(event.get("type") == "done" for event in events))
        self.assertEqual(len(model_client.calls), 1)
        self.assertTrue(model_client.results)
        self.assertFalse(any(model_client.results.values()))
        contents = [message.content for message in storage.messages]
        self.assertEqual(contents, ["hello", "ok"])
        self.assertFalse(any(
            (message.metadata or {}).get("steer") is True
            or message.type == MessageType.CONTEXT_EVENT
            for message in storage.messages
        ))

    async def test_steer_accepted_on_the_last_loop_is_answered_in_the_same_stream(self):
        storage = InMemoryMessageStorage()
        agent_box = {}
        model_client = _SteerDuringReplyModel(agent_box, first="draft", second="revised")
        agent = self._build_agent(storage, model_client)
        agent_box["agent"] = agent
        model_client.texts = ("alpha", "beta")

        events = [
            event
            async for event in agent.chat_events(
                user_message="work",
                user_id="alice",
                channel="cli",
                room_name="lab",
                max_agent_loops=1,
            )
        ]

        done = [event for event in events if event.get("type") == "message_done"]
        self.assertEqual(
            [(event.get("phase"), event.get("content")) for event in done],
            [("interim", "draft"), ("final", "revised")],
        )
        self.assertEqual([event.get("type") for event in events].count("done"), 1)
        self.assertEqual(len(model_client.calls), 2)
        self.assertNotIn("alpha", repr(model_client.calls[0]))
        self.assertIn("alpha", repr(model_client.calls[1]))
        self.assertIn("beta", repr(model_client.calls[1]))
        stored = _stored_steers(storage)
        self.assertEqual([message.content for message in stored], ["alpha", "beta"])
        self.assertFalse(any(
            (message.metadata or {}).get("deferred_steer") for message in storage.messages
        ))
        self.assertFalse(agent.inbox.busy)
        self.assertFalse(agent.inbox.has_pending_steer())

    async def test_abort_delivers_an_accepted_steer_in_the_same_stream(self):
        storage = InMemoryMessageStorage()
        agent_box = {}
        model_client = CapturingModelClient([
            (ReplyType.TOOL_CALL, [FakeToolCall(call_id="call-1")]),
            (ReplyType.SIMPLE_REPLY, "handled"),
        ])
        executor = _AbortAfterSteerExecutor(agent_box)
        agent = self._build_agent(storage, model_client, tool_executor=executor)
        agent_box["agent"] = agent

        events = [
            event
            async for event in agent.chat_events(
                user_message="work",
                user_id="alice",
                channel="cli",
                room_name="lab",
            )
        ]

        types = [event.get("type") for event in events]
        self.assertIn("message_done", types)
        self.assertLess(types.index("message_done"), types.index("aborted"))
        self.assertLess(types.index("aborted"), types.index("done"))
        self.assertEqual(types.count("done"), 1)
        handled = next(event for event in events if event.get("type") == "message_done")
        self.assertEqual(handled.get("content"), "handled")
        self.assertEqual(len(model_client.calls), 2)
        self.assertNotIn("check the logs", repr(model_client.calls[0]))
        self.assertIn("check the logs", repr(model_client.calls[1]))
        self.assertEqual([message.content for message in _stored_steers(storage)], ["check the logs"])
        self.assertFalse(agent.inbox.has_pending_steer())


class _AbortAfterSteerExecutor:
    def __init__(self, agent_box):
        self.agent_box = agent_box

    async def handle_tool_calls(self, tool_calls, input_messages, max_concurrent_tools, **kwargs):
        agent = self.agent_box["agent"]
        accepted = await agent.steer(
            agent.inbox.current_turn_id,
            "check the logs",
            channel="cli",
            user_id="alice",
            room_name="lab",
        )
        if not accepted:
            raise AssertionError("steer rejected before abort")
        agent.abort()
        call_id = getattr(tool_calls[0], "call_id", None) or "call-1"
        input_messages.extend([
            {"type": "function_call", "call_id": call_id, "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": call_id, "output": "ok"},
        ])
        return None


class _ResponsesBatchExecutor:
    def __init__(self):
        self.agent = None
        self.seen = []
        self._round = 0

    async def handle_tool_calls(self, tool_calls, input_messages, max_concurrent_tools, **kwargs):
        self._round += 1
        if self._round == 1:
            agent = self.agent
            turn_id = agent.inbox.current_turn_id
            accepted = [
                await agent.steer(
                    turn_id,
                    text,
                    channel="cli",
                    user_id="alice",
                    room_name="lab",
                )
                for text in ("check the logs", "then restart")
            ]
            if accepted != [True, True]:
                raise AssertionError(f"steer rejected during tool batch: {accepted}")
            if "check the logs" in repr(input_messages):
                raise AssertionError("steer was injected inside the tool batch")
        self.seen.append(list(input_messages))
        call_id = getattr(tool_calls[0], "call_id", None) or "call-1"
        input_messages.extend([
            {"type": "function_call", "call_id": call_id, "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": call_id, "output": f"result-{call_id}"},
        ])
        return None


class _SteerDuringReplyModel(CapturingModelClient):
    def __init__(self, agent_box, first="draft answer", second="revised answer"):
        super().__init__([
            (ReplyType.SIMPLE_REPLY, first),
            (ReplyType.SIMPLE_REPLY, second),
        ])
        self.agent_box = agent_box
        self.texts = ("use metric units", "round to two decimals")

    async def model_turn_events(self, messages, tool_specs, instructions=None, stream=False):
        self.calls.append(messages)
        self.instructions_calls.append(instructions)
        if len(self.calls) == 1:
            agent = self.agent_box["agent"]
            turn_id = agent.inbox.current_turn_id
            accepted = [
                await agent.steer(
                    turn_id,
                    text,
                    channel="cli",
                    user_id="alice",
                    room_name="lab",
                )
                for text in self.texts
            ]
            if accepted != [True, True]:
                raise AssertionError(f"steer rejected during final reply: {accepted}")
        _reply_type, response = self.responses.pop(0)
        yield ModelStreamEvent(type="delta", delta=str(response))


class _RejectingSteerModel(CapturingModelClient):
    def __init__(self, agent_box):
        super().__init__([(ReplyType.SIMPLE_REPLY, "ok")])
        self.agent_box = agent_box
        self.results = {}

    async def model_turn_events(self, messages, tool_specs, instructions=None, stream=False):
        self.calls.append(messages)
        self.instructions_calls.append(instructions)
        agent = self.agent_box["agent"]
        turn_id = agent.inbox.current_turn_id
        self.results = {
            "other_user": await agent.steer(
                turn_id, "from bob", channel="cli", user_id="bob", room_name="lab",
            ),
            "other_room": await agent.steer(
                turn_id, "other room", channel="cli", user_id="alice", room_name="other",
            ),
            "other_channel": await agent.steer(
                turn_id, "other channel", channel="api", user_id="alice", room_name="lab",
            ),
            "missing_user": await agent.steer(
                turn_id, "no user", channel="cli", room_name="lab",
            ),
            "wrong_turn": await agent.steer(
                "someone-else", "nope", channel="cli", user_id="alice", room_name="lab",
            ),
        }
        yield ModelStreamEvent(type="delta", delta="ok")


class SteerQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_steer_requires_same_user_and_room(self):
        inbox = AgentInbox()
        ticket = inbox.reserve_turn(channel="cli", user_id="alice", room_name="lab")
        await inbox.acquire_turn(ticket)
        try:
            self.assertFalse(inbox.request_steer(
                ticket.turn_id, " ", channel="cli", user_id="alice", room_name="lab",
            ))
            self.assertFalse(inbox.request_steer(
                "other", "hi", channel="cli", user_id="alice", room_name="lab",
            ))
            self.assertFalse(inbox.request_steer(
                ticket.turn_id, "hi", channel="api", user_id="alice", room_name="lab",
            ))
            self.assertFalse(inbox.request_steer(
                ticket.turn_id, "hi", channel="cli", user_id="bob", room_name="lab",
            ))
            self.assertFalse(inbox.request_steer(
                ticket.turn_id, "hi", channel="cli", user_id="alice", room_name="other",
            ))
            self.assertFalse(inbox.request_steer(
                ticket.turn_id, "hi", channel="cli", user_id="alice",
            ))
            self.assertFalse(inbox.request_steer(
                ticket.turn_id, "hi", channel="cli", room_name="lab",
            ))
            self.assertTrue(inbox.request_steer(
                ticket.turn_id, " one ", channel="cli", user_id="alice", room_name="lab",
            ))
            self.assertTrue(inbox.request_steer(
                ticket.turn_id, "two", channel="cli", user_id="alice", room_name="lab",
            ))
            self.assertEqual(inbox.take_steering(), ["one", "two"])
            self.assertFalse(inbox.has_pending_steer())
        finally:
            inbox.release_turn()

    async def test_release_closes_the_turn(self):
        inbox = AgentInbox()
        ticket = inbox.reserve_turn(channel="cli", user_id="alice", room_name="lab")
        await inbox.acquire_turn(ticket)
        self.assertTrue(inbox.request_steer(
            ticket.turn_id, "one", channel="cli", user_id="alice", room_name="lab",
        ))
        inbox.release_turn()
        self.assertFalse(inbox.busy)
        self.assertIsNone(inbox.current_turn_id)
        self.assertFalse(inbox.has_pending_steer())
        self.assertFalse(inbox.request_steer(
            ticket.turn_id, "two", channel="cli", user_id="alice", room_name="lab",
        ))

    async def test_roomless_turn_matches_blank_room_only(self):
        inbox = AgentInbox()
        ticket = inbox.reserve_turn(channel="cli", user_id="alice")
        await inbox.acquire_turn(ticket)
        try:
            self.assertFalse(inbox.request_steer(
                ticket.turn_id, "no", channel="cli", user_id="alice", room_name="lab",
            ))
            self.assertTrue(inbox.request_steer(
                ticket.turn_id, "yes", channel="cli", user_id="alice",
            ))
        finally:
            inbox.release_turn()


class _HoldTextModel(CapturingModelClient):
    """Block the first model call, then answer every call with plain text."""

    def __init__(self):
        super().__init__([])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def model_turn_events(self, messages, tool_specs, instructions=None, stream=False):
        self.calls.append(messages)
        self.instructions_calls.append(instructions)
        if len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
        yield ModelStreamEvent(type="delta", delta="ok")


class _HoldToolModel(_HoldTextModel):
    async def model_turn_events(self, messages, tool_specs, instructions=None, stream=False):
        self.calls.append(messages)
        self.instructions_calls.append(instructions)
        if len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
            yield ModelStreamEvent(
                type="tool_calls",
                tool_calls=[ChatToolCall(call_id="call-1", name="lookup", arguments="{}")],
            )
            return
        yield ModelStreamEvent(type="delta", delta="ok")


class _AdjacentPairExecutor:
    async def handle_tool_calls(self, tool_calls, input_messages, max_concurrent_tools, **kwargs):
        call_id = getattr(tool_calls[0], "call_id", None) or "call-1"
        input_messages.extend([
            {"type": "function_call", "call_id": call_id, "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": call_id, "output": "ok"},
        ])
        return None


class BusyInputRoutingTests(unittest.IsolatedAsyncioTestCase):
    _build_agent = AgentInboxTests._build_agent

    async def _collect(self, agent, text, **kwargs):
        options = {"user_id": "alice", "channel": "cli", "room_name": "lab"}
        options.update(kwargs)
        return [
            event
            async for event in agent.chat_events(user_message=text, **options)
        ]

    async def _wait_until(self, predicate, steps: int = 100):
        for _ in range(steps):
            if predicate():
                return
            await asyncio.sleep(0.001)
        self.assertTrue(predicate())

    async def test_same_session_busy_message_is_steered(self):
        storage = InMemoryMessageStorage()
        model = _HoldToolModel()
        agent = self._build_agent(storage, model, tool_executor=_AdjacentPairExecutor())

        first = asyncio.create_task(self._collect(agent, "inspect the service", sender_name="Alice"))
        await model.started.wait()
        second_events = await asyncio.wait_for(
            self._collect(agent, "check the logs", sender_name="Alice"),
            timeout=1,
        )

        self.assertEqual([event.get("type") for event in second_events], ["steered", "done"])
        self.assertEqual(second_events[0]["turn_id"], agent.inbox.current_turn_id)
        self.assertEqual(second_events[0]["user_id"], "alice")
        self.assertEqual(second_events[0]["room_name"], "lab")
        self.assertEqual(second_events[0]["content"], "check the logs")
        self.assertTrue(second_events[1].get("steered"))
        self.assertFalse(any(event.get("type") == "message_done" for event in second_events))
        self.assertEqual([message.content for message in storage.messages], ["inspect the service"])

        model.release.set()
        first_events = await first

        self.assertTrue(any(event.get("type") == "tool_result" for event in first_events))
        self.assertEqual(len(model.calls), 2)
        self.assertNotIn("check the logs", repr(model.calls[0]))
        self.assertEqual(model.instructions_calls[0], model.instructions_calls[1])
        _assert_tool_pairs_intact(self, model.calls[1])
        tail = model.calls[1][-3:]
        self.assertEqual(tail[0].get("type"), "function_call")
        self.assertEqual(tail[1].get("type"), "function_call_output")
        self.assertEqual(tail[1].get("call_id"), tail[0].get("call_id"))
        self.assertEqual(tail[2]["content"], "check the logs")
        stored = _stored_steers(storage)
        self.assertEqual([message.content for message in stored], ["check the logs"])
        self.assertFalse(agent.inbox.busy)
        self.assertFalse(agent.inbox.has_pending_steer())

    async def test_other_user_or_room_stays_queued(self):
        storage = InMemoryMessageStorage()
        model = _HoldTextModel()
        agent = self._build_agent(storage, model)

        first = asyncio.create_task(self._collect(agent, "hello"))
        await model.started.wait()
        other_user = asyncio.create_task(self._collect(agent, "from bob", user_id="bob"))
        other_room = asyncio.create_task(self._collect(agent, "other room", room_name="kitchen"))
        await self._wait_until(lambda: agent.inbox.pending_count == 2 and not other_user.done() and not other_room.done())

        self.assertFalse(other_user.done())
        self.assertFalse(other_room.done())
        self.assertEqual(agent.inbox.pending_count, 2)
        queued = [message.content for message in storage.messages if message.role == RoleType.USER]
        self.assertEqual(queued, ["hello", "from bob", "other room"])
        self.assertTrue(all(
            message.metadata.get(INBOX_KIND_METADATA_KEY) == InboxKind.USER_TURN.value
            for message in storage.messages
            if message.role == RoleType.USER
        ))
        self.assertNotIn("from bob", repr(model.calls[0]))
        self.assertNotIn("other room", repr(model.calls[0]))

        model.release.set()
        first_events, user_events, room_events = await asyncio.gather(first, other_user, other_room)

        self.assertEqual(len(model.calls), 3)
        self.assertNotIn("from bob", repr(model.calls[0]))
        self.assertNotIn("other room", repr(model.calls[0]))
        self.assertIn("from bob", repr(model.calls[1]))
        self.assertIn("other room", repr(model.calls[2]))
        self.assertFalse(any(
            event.get("type") == "steered"
            for event in [*first_events, *user_events, *room_events]
        ))
        self.assertFalse(agent.inbox.busy)

    async def test_rejected_steer_falls_back_to_the_queue(self):
        storage = InMemoryMessageStorage()
        model = _HoldTextModel()
        agent = self._build_agent(storage, model)

        async def reject(*args, **kwargs):
            return False

        agent.steer = reject
        first = asyncio.create_task(self._collect(agent, "first"))
        await model.started.wait()
        second = asyncio.create_task(self._collect(agent, "still here"))
        await self._wait_until(lambda: agent.inbox.pending_count == 1 and not second.done())

        self.assertFalse(second.done())
        self.assertEqual(
            [message.content for message in storage.messages if message.role == RoleType.USER],
            ["first", "still here"],
        )
        self.assertFalse(agent.inbox.has_pending_steer())

        model.release.set()
        first_events, second_events = await asyncio.gather(first, second)

        self.assertEqual(len(model.calls), 2)
        self.assertIn("still here", repr(model.calls[1]))
        self.assertFalse(any(event.get("type") == "steered" for event in [*first_events, *second_events]))
        self.assertEqual(
            storage.messages[1].metadata.get(INBOX_KIND_METADATA_KEY),
            InboxKind.USER_TURN.value,
        )
        self.assertFalse(agent.inbox.busy)

    async def test_queued_message_outlasts_a_full_turn(self):
        self.assertGreaterEqual(
            AgentConfig.DEFAULT_TURN_QUEUE_TIMEOUT,
            AgentConfig.DEFAULT_TURN_RUN_TIMEOUT,
        )
        self.assertEqual(
            AgentConfig.DEFAULT_TURN_QUEUE_TIMEOUT,
            AgentConfig.DEFAULT_TURN_RUN_TIMEOUT + AgentConfig.DEFAULT_TURN_QUEUE_GRACE_SECONDS,
        )
        run_timeout = 0.2
        inbox = AgentInbox(
            run_timeout=run_timeout,
            queue_timeout=run_timeout + AgentConfig.DEFAULT_TURN_QUEUE_GRACE_SECONDS,
        )
        holder = inbox.reserve_turn(channel="cli", user_id="alice", room_name="lab")
        await inbox.acquire_turn(holder)
        waiter = inbox.reserve_turn(channel="api", user_id="bob", room_name="other")

        async def release_after_budget():
            await asyncio.sleep(run_timeout)
            inbox.release_turn()

        releasing = asyncio.create_task(release_after_budget())
        await asyncio.wait_for(inbox.acquire_turn(waiter), timeout=1)
        self.assertEqual(inbox.current_turn_id, waiter.turn_id)
        inbox.release_turn()
        await releasing


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
