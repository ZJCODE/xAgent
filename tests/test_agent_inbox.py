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


def _steer_model_texts(messages) -> list[str]:
    texts = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.startswith("[Mid-task message from "):
            texts.append(content)
    return texts


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
        self.assertEqual(_steer_model_texts(model_client.calls[0]), [])
        first_tail = _steer_model_texts(model_client.calls[1])
        second_tail = _steer_model_texts(model_client.calls[2])
        self.assertEqual(len(first_tail), 2)
        self.assertEqual(first_tail, second_tail)
        self.assertRegex(
            first_tail[0],
            r"^\[Mid-task message from Alice at \d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\ncheck the logs$",
        )
        self.assertRegex(
            first_tail[1],
            r"^\[Mid-task message from Alice at \d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\nthen restart$",
        )
        self.assertEqual(model_client.calls[1][-2:], [
            {"role": "user", "content": first_tail[0]},
            {"role": "user", "content": first_tail[1]},
        ])
        self.assertEqual(model_client.calls[1][-4]["type"], "function_call")
        self.assertEqual(model_client.calls[1][-3]["type"], "function_call_output")
        for call in model_client.calls:
            _assert_tool_pairs_intact(self, call)
            self.assertEqual(call[:len(model_client.calls[0])], model_client.calls[0])
        self.assertEqual(model_client.instructions_calls[0], model_client.instructions_calls[1])
        self.assertEqual(model_client.instructions_calls[0], model_client.instructions_calls[2])
        self.assertNotIn("check the logs", repr(model_client.instructions_calls[0]))
        self.assertNotIn("Mid-task", repr(model_client.instructions_calls[0]))
        self.assertNotIn("check the logs", repr(executor.seen[0]))

        stored = [
            message for message in storage.messages
            if (message.metadata or {}).get(INBOX_KIND_METADATA_KEY) == InboxKind.STEER.value
        ]
        self.assertEqual([message.content for message in stored], ["check the logs", "then restart"])
        self.assertTrue(all(
            message.type == MessageType.MESSAGE and message.role == RoleType.USER and message.metadata.get("steer") is True
            for message in stored
        ))
        self.assertFalse(any(message.type == MessageType.CONTEXT_EVENT for message in storage.messages))
        experience_ids = {id(message) for message in agent.memory_handler.experience_messages}
        self.assertTrue(all(id(message) in experience_ids for message in stored))

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
        self.assertEqual(_steer_model_texts(model_client.calls[0]), [])
        self.assertEqual(
            model_client.calls[1][:len(model_client.calls[0])],
            model_client.calls[0],
        )
        added = model_client.calls[1][len(model_client.calls[0]):]
        self.assertEqual(added[0], {"role": "assistant", "content": "draft answer"})
        self.assertEqual(len(added), 3)
        self.assertIn("use metric units", added[1]["content"])
        self.assertIn("round to two decimals", added[2]["content"])
        self.assertEqual(model_client.instructions_calls[0], model_client.instructions_calls[1])
        self.assertNotIn("use metric units", repr(model_client.instructions_calls[0]))

        stored = [
            message for message in storage.messages
            if (message.metadata or {}).get(INBOX_KIND_METADATA_KEY) == InboxKind.STEER.value
        ]
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
        experience_ids = {id(message) for message in agent.memory_handler.experience_messages}
        self.assertTrue(all(id(message) in experience_ids for message in stored))
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
            (message.metadata or {}).get(INBOX_KIND_METADATA_KEY) == InboxKind.STEER.value
            or (message.metadata or {}).get("event_type") == "steer"
            or message.type == MessageType.CONTEXT_EVENT
            for message in storage.messages
        ))

    async def test_leftover_steer_becomes_followup_turn(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "done"),
            (ReplyType.SIMPLE_REPLY, "followed up"),
        ])
        agent = self._build_agent(storage, model_client)
        original = agent.message_handler.store_model_reply
        state = {"done": False}

        async def store_model_reply(*args, **kwargs):
            message = await original(*args, **kwargs)
            metadata = kwargs.get("metadata") or {}
            if metadata.get("turn_phase") == "final" and not state["done"]:
                state["done"] = True
                turn_id = agent.inbox.current_turn_id
                accepted = [
                    agent.inbox.request_steer(
                        turn_id,
                        text,
                        channel="cli",
                        user_id="alice",
                        room_name="lab",
                        sender_name="Alice",
                    )
                    for text in ("alpha", "beta")
                ]
                if accepted != [True, True]:
                    raise AssertionError(f"leftover steer was rejected: {accepted}")
            return message

        agent.message_handler.store_model_reply = store_model_reply

        events = [
            event
            async for event in agent.chat_events(
                user_message="work",
                user_id="alice",
                channel="cli",
                room_name="lab",
            )
        ]

        self.assertEqual([event.get("type") for event in events].count("done"), 1)
        self.assertEqual(len(model_client.calls), 2)
        self.assertNotIn("alpha", repr(model_client.calls[0]))
        self.assertIn("alpha", repr(model_client.calls[1]))
        self.assertIn("beta", repr(model_client.calls[1]))
        follow_up = next(
            message for message in storage.messages
            if message.role == RoleType.USER and "alpha" in message.content
        )
        self.assertEqual(follow_up.content, "alpha\n\nbeta")
        self.assertTrue(follow_up.metadata.get("deferred_steer"))
        self.assertEqual(
            follow_up.metadata.get(INBOX_KIND_METADATA_KEY),
            InboxKind.USER_TURN.value,
        )
        self.assertIn(follow_up, agent.memory_handler.experience_messages)
        self.assertFalse(agent.inbox.busy)
        self.assertFalse(agent.inbox.has_pending_steer())


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
                    sender_name="Alice",
                )
                for text in ("check the logs", "then restart")
            ]
            if accepted != [True, True]:
                raise AssertionError(f"steer rejected during tool batch: {accepted}")
            if "check the logs" in repr(input_messages) or "Mid-task" in repr(input_messages):
                raise AssertionError("steer was injected inside the tool batch")
        self.seen.append(list(input_messages))
        call_id = getattr(tool_calls[0], "call_id", None) or "call-1"
        input_messages.extend([
            {"type": "function_call", "call_id": call_id, "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": call_id, "output": f"result-{call_id}"},
        ])
        return None


class _SteerDuringReplyModel(CapturingModelClient):
    def __init__(self, agent_box):
        super().__init__([
            (ReplyType.SIMPLE_REPLY, "draft answer"),
            (ReplyType.SIMPLE_REPLY, "revised answer"),
        ])
        self.agent_box = agent_box

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
                    sender_name="Alice",
                )
                for text in ("use metric units", "round to two decimals")
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
                sender_name="Alice",
            ))
            self.assertTrue(inbox.request_steer(
                ticket.turn_id, "two", channel="cli", user_id="alice", room_name="lab",
            ))
            pending = inbox.take_steering()
            self.assertEqual([item.content for item in pending], ["one", "two"])
            self.assertEqual(pending[0].sender_name, "Alice")
            self.assertFalse(inbox.has_pending_steer())
        finally:
            inbox.release_turn()

    async def test_release_returns_pending_steers(self):
        inbox = AgentInbox()
        ticket = inbox.reserve_turn(channel="cli", user_id="alice", room_name="lab")
        await inbox.acquire_turn(ticket)
        self.assertTrue(inbox.request_steer(
            ticket.turn_id, "one", channel="cli", user_id="alice", room_name="lab",
        ))
        self.assertTrue(inbox.request_steer(
            ticket.turn_id, "two", channel="cli", user_id="alice", room_name="lab",
        ))
        pending = inbox.release_turn()
        self.assertEqual([item.content for item in pending], ["one", "two"])
        self.assertFalse(inbox.busy)
        self.assertIsNone(inbox.current_turn_id)
        self.assertEqual(inbox.take_steering(), [])

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
