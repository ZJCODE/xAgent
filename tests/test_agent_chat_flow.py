import unittest
from types import SimpleNamespace

from pydantic import BaseModel

from xagent.components.message import MessageStorageBase
from xagent.core.agent import Agent
from xagent.core.config import AgentConfig, ReplyType
from xagent.core.handlers.model import ChatToolCall, ModelClient
from xagent.core.handlers.message import MessageHandler
from xagent.core.tools.executor import ToolExecutor
from xagent.integrations.langfuse import NoopObservabilityRuntime
from xagent.schemas import Message, MessageType, RoleType


class InMemoryMessageStorage(MessageStorageBase):
    def __init__(self, initial_messages=None):
        self.messages = list(initial_messages or [])
        self.last_count = None
        self.last_offset = None

    async def add_messages(self, messages, **kwargs) -> None:
        if isinstance(messages, list):
            self.messages.extend(messages)
        else:
            self.messages.append(messages)

    async def get_messages(self, count: int = 20, offset: int = 0):
        self.last_count = count
        self.last_offset = offset
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
    def __init__(self, tools=None):
        self._tools = dict(tools or {})
        self.cached_tool_specs = None

    def get_tool(self, name):
        return self._tools.get(name)


class FakeMemoryHandler:
    def __init__(self):
        self.scheduled_messages = None
        self.experience_messages = None
        self.caused_reply = None

    async def get_recent_context(self):
        return ""

    def schedule_diary_write(self, messages):
        self.scheduled_messages = messages

    def schedule_experience_write(self, messages, caused_reply=False):
        self.experience_messages = messages
        self.caused_reply = caused_reply


class FakeObservabilityRuntime:
    enabled = True

    def __init__(self):
        self.turn_kwargs = None
        self.entered = False
        self.exited = False
        self.flushed = False

    def create_client(self, client_kwargs):
        return None

    def agent_turn(self, **kwargs):
        self.turn_kwargs = kwargs
        return self

    def __enter__(self):
        self.entered = True

    def __exit__(self, exc_type, exc, traceback):
        self.exited = True

    async def flush(self):
        self.flushed = True


class CapturingModelClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.instructions_calls = []

    async def call(self, messages, tool_specs, instructions=None, output_type=None, stream=False, store_reply=None):
        self.calls.append(messages)
        self.instructions_calls.append(instructions)
        return self.responses.pop(0)


class FakeToolExecutor:
    def __init__(self):
        self.seen_input_messages = []

    async def handle_tool_calls(self, tool_calls, input_messages, max_concurrent_tools):
        self.seen_input_messages.append(list(input_messages))
        input_messages.extend([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "lookup result"},
        ])
        return None


class FakeToolCall:
    def __init__(self, name="lookup", arguments="{}", call_id="call-1"):
        self.type = "function"
        self.name = name
        self.arguments = arguments
        self.call_id = call_id


class FakeChatCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeOpenAIClient:
    def __init__(self, responses):
        self.chat_completions = FakeChatCompletions(responses)
        self.chat = SimpleNamespace(completions=self.chat_completions)


class AsyncChunkStream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration


def _chat_response(content=None, tool_calls=None, reasoning_content=None):
    message = SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        reasoning_content=reasoning_content,
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


class StructuredAnswer(BaseModel):
    answer: str


class ModelClientResponseTests(unittest.IsolatedAsyncioTestCase):
    def test_non_stream_response_prioritizes_tool_calls_over_text(self):
        raw_tool_call = SimpleNamespace(
            id="call-1",
            type="function",
            function=SimpleNamespace(name="lookup", arguments="{}"),
        )
        response = _chat_response(content="I will look that up first.", tool_calls=[raw_tool_call])

        reply_type, payload = ModelClient._handle_non_stream(response)

        self.assertEqual(reply_type, ReplyType.TOOL_CALL)
        self.assertEqual(payload, [ChatToolCall(call_id="call-1", name="lookup", arguments="{}")])

    def test_non_stream_tool_calls_preserve_reasoning_content(self):
        raw_tool_call = SimpleNamespace(
            id="call-1",
            type="function",
            function=SimpleNamespace(name="lookup", arguments="{}"),
        )
        response = _chat_response(
            tool_calls=[raw_tool_call],
            reasoning_content="I need to inspect local state before answering.",
        )

        reply_type, payload = ModelClient._handle_non_stream(response)

        self.assertEqual(reply_type, ReplyType.TOOL_CALL)
        self.assertEqual(payload[0].reasoning_content, "I need to inspect local state before answering.")

    async def test_call_uses_chat_completions_with_system_message(self):
        client = FakeOpenAIClient([_chat_response(content="ok")])
        model = ModelClient(client=client, model="test-model")

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "hello"}],
            tool_specs=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            instructions="Core Rules",
        )

        self.assertEqual(reply_type, ReplyType.SIMPLE_REPLY)
        self.assertEqual(payload, "ok")
        call = client.chat_completions.calls[0]
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["messages"][0], {"role": "system", "content": "Core Rules"})
        self.assertEqual(call["messages"][1], {"role": "user", "content": "hello"})
        self.assertEqual(call["tool_choice"], "auto")

    async def test_structured_output_uses_json_object_and_pydantic_validation(self):
        client = FakeOpenAIClient([_chat_response(content='{"answer": "ok"}')])
        model = ModelClient(client=client, model="test-model")

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "answer as json"}],
            tool_specs=None,
            instructions="Return JSON",
            output_type=StructuredAnswer,
        )

        self.assertEqual(reply_type, ReplyType.STRUCTURED_REPLY)
        self.assertEqual(payload.answer, "ok")
        call = client.chat_completions.calls[0]
        self.assertEqual(call["response_format"], {"type": "json_object"})
        self.assertIn("JSON schema", call["messages"][0]["content"])

    async def test_stream_text_yields_chunks_and_stores_final_text(self):
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hel", tool_calls=None))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="lo", tool_calls=None))]),
        ]
        client = FakeOpenAIClient([AsyncChunkStream(chunks)])
        model = ModelClient(client=client, model="test-model")
        stored = []

        async def store_reply(text):
            stored.append(text)

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "hello"}],
            tool_specs=None,
            stream=True,
            store_reply=store_reply,
        )

        self.assertEqual(reply_type, ReplyType.SIMPLE_REPLY)
        collected = []
        async for chunk in payload:
            collected.append(chunk)
        self.assertEqual(collected, ["Hel", "lo"])
        self.assertEqual(stored, ["Hello"])

    async def test_stream_tool_calls_accumulates_split_arguments(self):
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                reasoning_content="I should call ",
                tool_calls=None,
                content=None,
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=[
                SimpleNamespace(index=0, id="call-1", type="function", function=SimpleNamespace(name="lookup", arguments='{"value"'))
            ], content=None, reasoning_content="the lookup tool."))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=[
                SimpleNamespace(index=0, id=None, type=None, function=SimpleNamespace(name=None, arguments=': "ok"}'))
            ], content=None, reasoning_content=None))]),
        ]
        client = FakeOpenAIClient([AsyncChunkStream(chunks)])
        model = ModelClient(client=client, model="test-model")

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "lookup"}],
            tool_specs=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            stream=True,
        )

        self.assertEqual(reply_type, ReplyType.TOOL_CALL)
        self.assertEqual(
            payload,
            [ChatToolCall(
                call_id="call-1",
                name="lookup",
                arguments='{"value": "ok"}',
                reasoning_content="I should call the lookup tool.",
            )],
        )


class AgentChatFlowTests(unittest.IsolatedAsyncioTestCase):
    def _build_agent(
        self,
        storage,
        model_client,
        tool_executor=None,
        tools=None,
        memory_handler=None,
        observability=None,
    ):
        agent = Agent.__new__(Agent)
        agent.model = AgentConfig.DEFAULT_MODEL
        agent.output_type = None
        agent.system_prompt = ""
        agent._assistant_sender_id = "agent"
        agent._private_handler = None
        agent.observability = observability or NoopObservabilityRuntime()
        agent.tool_manager = FakeToolManager(tools=tools)
        agent.model_client = model_client
        agent.message_storage = storage
        agent.message_handler = MessageHandler(message_storage=storage, system_prompt="")
        agent.memory_handler = memory_handler or FakeMemoryHandler()
        agent.tool_executor = tool_executor or FakeToolExecutor()
        return agent

    async def test_chat_keeps_tool_messages_transient_inside_loop(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([
            (ReplyType.TOOL_CALL, [FakeToolCall()]),
            (ReplyType.SIMPLE_REPLY, "Done"),
        ])
        tool_executor = FakeToolExecutor()
        agent = self._build_agent(
            storage=storage,
            model_client=model_client,
            tool_executor=tool_executor,
        )

        result = await Agent.chat(
            agent,
            user_message="Run the lookup and summarize it",
            user_id="bob",
            history_count=10,
            max_iter=3,
            enable_memory=False,
        )

        self.assertEqual(result, "Done")
        self.assertEqual(len(model_client.calls), 2)
        first_call_messages = model_client.calls[0]
        second_call_messages = model_client.calls[1]
        # No system message — input starts with transcript user message
        self.assertEqual(len(first_call_messages), 1)
        self.assertEqual(second_call_messages[1]["role"], "assistant")
        self.assertEqual(second_call_messages[1]["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(second_call_messages[2]["role"], "tool")
        self.assertEqual(second_call_messages[2]["tool_call_id"], "call-1")
        self.assertEqual(
            [message.role for message in storage.messages],
            [RoleType.USER, RoleType.ASSISTANT],
        )

    async def test_chat_wraps_turn_in_observability_context(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "Traced answer"),
        ])
        observability = FakeObservabilityRuntime()
        agent = self._build_agent(
            storage=storage,
            model_client=model_client,
            observability=observability,
        )

        result = await Agent.chat(
            agent,
            user_message="trace this",
            user_id="alice",
            stream=True,
            private=True,
        )

        self.assertEqual(result, "Traced answer")
        self.assertTrue(observability.entered)
        self.assertTrue(observability.exited)
        self.assertEqual(observability.turn_kwargs["user_id"], "alice")
        self.assertEqual(observability.turn_kwargs["model"], AgentConfig.DEFAULT_MODEL)
        self.assertTrue(observability.turn_kwargs["private"])
        self.assertEqual(observability.turn_kwargs["memory_mode"], "read_only")
        self.assertTrue(observability.turn_kwargs["stream"])

    async def test_flush_memory_flushes_observability(self):
        storage = InMemoryMessageStorage()
        observability = FakeObservabilityRuntime()
        agent = self._build_agent(
            storage=storage,
            model_client=CapturingModelClient([]),
            observability=observability,
        )

        await Agent.flush_memory(agent)

        self.assertTrue(observability.flushed)

    async def test_chat_caps_history_before_loading_messages(self):
        storage = InMemoryMessageStorage([
            Message.create(f"old-{index:02d}", role=RoleType.USER, sender_id="alice")
            for index in range(50)
        ])
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "Final answer"),
        ])
        agent = self._build_agent(storage=storage, model_client=model_client)

        result = await Agent.chat(
            agent,
            user_message="latest request",
            user_id="alice",
            max_iter=2,
            enable_memory=False,
        )

        self.assertEqual(result, "Final answer")
        self.assertEqual(storage.last_count, AgentConfig.MAX_TRANSCRIPT_MESSAGES)
        transcript = model_client.calls[0][0]["content"]
        self.assertNotIn("old-00", transcript)
        self.assertNotIn("old-10", transcript)
        self.assertIn("old-49", transcript)
        self.assertIn("latest request", transcript)

    async def test_transcript_budget_omits_older_messages_and_truncates_content(self):
        messages = [
            Message.create(f"message-{index}", role=RoleType.USER, sender_id="alice")
            for index in range(3)
        ]
        messages.append(
            Message.create("x" * 30, role=RoleType.USER, sender_id="alice")
        )

        transcript = MessageHandler.build_recent_transcript_message(
            messages,
            current_user_id="alice",
            max_messages=2,
            max_total_chars=200,
            max_message_chars=10,
        )["content"]

        self.assertIn("[Earlier experience omitted: 2 conversation messages]", transcript)
        self.assertNotIn("message-0", transcript)
        self.assertIn("message-2", transcript)
        self.assertIn("[Content truncated: 20 chars omitted]", transcript)

    async def test_transcript_budget_preserves_latest_user_images(self):
        image_url = "https://example.com/chart.png"
        messages = [
            Message.create("older", role=RoleType.USER, sender_id="alice"),
            Message.create("look at this", role=RoleType.USER, sender_id="alice", image_source=image_url),
        ]

        model_message = MessageHandler.build_recent_transcript_message(
            messages,
            current_user_id="alice",
            max_messages=1,
            max_total_chars=200,
            max_message_chars=100,
        )

        self.assertIsInstance(model_message["content"], list)
        self.assertEqual(model_message["content"][1]["image_url"]["url"], image_url)

    async def test_observe_ingests_event_without_calling_model(self):
        storage = InMemoryMessageStorage()
        memory_handler = FakeMemoryHandler()
        model_client = CapturingModelClient([])
        agent = self._build_agent(
            storage=storage,
            model_client=model_client,
            memory_handler=memory_handler,
        )

        result = await Agent.observe(
            agent,
            context="看到有人靠近门口。",
            source="camera",
            event_type="presence",
        )

        self.assertFalse(result.replied)
        self.assertIsNone(result.reply)
        self.assertEqual(len(storage.messages), 1)
        self.assertEqual(storage.messages[0].type, MessageType.CONTEXT_EVENT)
        self.assertEqual(storage.messages[0].metadata["source"], "camera")
        self.assertEqual(storage.messages[0].metadata["event_type"], "presence")
        self.assertEqual(model_client.calls, [])
        self.assertEqual(memory_handler.experience_messages, [storage.messages[0]])
        self.assertFalse(memory_handler.caused_reply)

    async def test_observe_stores_ingested_history_recap(self):
        storage = InMemoryMessageStorage()
        memory_handler = FakeMemoryHandler()
        model_client = CapturingModelClient([])
        agent = self._build_agent(
            storage=storage,
            model_client=model_client,
            memory_handler=memory_handler,
        )

        result = await Agent.observe(
            agent,
            context="预拉取的群历史 recap。",
            source="feishu",
            event_type="history_recap",
        )

        self.assertFalse(result.replied)
        self.assertIsNone(result.reply)
        self.assertEqual(len(storage.messages), 1)
        self.assertEqual(storage.messages[0].type, MessageType.CONTEXT_EVENT)
        self.assertEqual(model_client.calls, [])
        self.assertEqual(memory_handler.experience_messages, [storage.messages[0]])
        self.assertFalse(memory_handler.caused_reply)

    async def test_observe_preserves_overheard_attribution_in_metadata(self):
        storage = InMemoryMessageStorage()
        memory_handler = FakeMemoryHandler()
        model_client = CapturingModelClient([])
        agent = self._build_agent(
            storage=storage,
            model_client=model_client,
            memory_handler=memory_handler,
        )

        result = await Agent.observe(
            agent,
            context="Bob 说活动可能要提前开始。",
            source="microphone",
            event_type="overheard_speech",
            metadata={"speaker_id": "bob", "addressed_to_agent": False},
        )

        self.assertFalse(result.replied)
        self.assertIsNone(result.reply)
        self.assertEqual([message.role for message in storage.messages], [RoleType.ENVIRONMENT])
        self.assertIsNone(storage.messages[0].sender_id)
        self.assertEqual(storage.messages[0].metadata["speaker_id"], "bob")
        self.assertEqual(model_client.calls, [])
        self.assertEqual(memory_handler.experience_messages, storage.messages)
        self.assertFalse(memory_handler.caused_reply)


class ToolExecutorTransientTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_single_does_not_persist_tool_messages(self):
        async def lookup(value: str) -> dict:
            return {"value": value}

        storage = InMemoryMessageStorage()
        executor = ToolExecutor(
            tool_manager=FakeToolManager(tools={"lookup": lookup}),
            message_storage=storage,
            client=None,
        )

        tool_message, image_data, description = await executor.execute_single(
            FakeToolCall(name="lookup", arguments='{"value": "ok"}')
        )

        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "call-1")
        self.assertEqual(tool_message["content"], '{"value": "ok"}')
        self.assertIsNone(image_data)
        self.assertIsNone(description)
        self.assertEqual(storage.messages, [])

    async def test_handle_tool_calls_appends_standard_assistant_and_tool_messages(self):
        async def first() -> str:
            return "one"

        async def second() -> str:
            return "two"

        storage = InMemoryMessageStorage()
        executor = ToolExecutor(
            tool_manager=FakeToolManager(tools={"first": first, "second": second}),
            message_storage=storage,
            client=None,
        )
        messages = [{"role": "user", "content": "run tools"}]

        result = await executor.handle_tool_calls(
            [
                FakeToolCall(name="first", call_id="call-1"),
                FakeToolCall(name="second", call_id="call-2"),
            ],
            messages,
            max_concurrent_tools=2,
        )

        self.assertIsNone(result)
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(len(messages[1]["tool_calls"]), 2)
        self.assertEqual(messages[2], {"role": "tool", "tool_call_id": "call-1", "content": "one"})
        self.assertEqual(messages[3], {"role": "tool", "tool_call_id": "call-2", "content": "two"})

    async def test_handle_tool_calls_appends_reasoning_content_when_present(self):
        async def lookup() -> str:
            return "ok"

        storage = InMemoryMessageStorage()
        executor = ToolExecutor(
            tool_manager=FakeToolManager(tools={"lookup": lookup}),
            message_storage=storage,
            client=None,
        )
        messages = [{"role": "user", "content": "run tool"}]

        result = await executor.handle_tool_calls(
            [FakeToolCall(name="lookup", call_id="call-1")],
            messages,
            max_concurrent_tools=1,
        )

        self.assertIsNone(result)
        self.assertNotIn("reasoning_content", messages[1])

        messages = [{"role": "user", "content": "run tool"}]
        result = await executor.handle_tool_calls(
            [ChatToolCall(
                call_id="call-1",
                name="lookup",
                arguments="{}",
                reasoning_content="I need this tool result before answering.",
            )],
            messages,
            max_concurrent_tools=1,
        )

        self.assertIsNone(result)
        self.assertEqual(messages[1]["reasoning_content"], "I need this tool result before answering.")


if __name__ == "__main__":
    unittest.main()
