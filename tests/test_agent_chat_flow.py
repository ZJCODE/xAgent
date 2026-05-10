import unittest
from types import SimpleNamespace

from pydantic import BaseModel

from xagent.components.message.base_messages import MessageStorageBase
from xagent.core.agent import Agent
from xagent.core.config import ReplyType
from xagent.core.handlers.model import ChatToolCall, ModelClient
from xagent.core.handlers.message import MessageHandler
from xagent.core.tools.executor import ToolExecutor
from xagent.schemas import Message, RoleType


class InMemoryMessageStorage(MessageStorageBase):
    def __init__(self, initial_messages=None):
        self.messages = list(initial_messages or [])

    async def add_messages(self, messages, **kwargs) -> None:
        if isinstance(messages, list):
            self.messages.extend(messages)
        else:
            self.messages.append(messages)

    async def get_messages(self, count: int = 20):
        return self.messages[-count:]

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

    async def get_recent_context(self):
        return ""

    def schedule_diary_write(self, messages):
        self.scheduled_messages = messages


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


def _chat_response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
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
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=[
                SimpleNamespace(index=0, id="call-1", type="function", function=SimpleNamespace(name="lookup", arguments='{"value"'))
            ], content=None))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=[
                SimpleNamespace(index=0, id=None, type=None, function=SimpleNamespace(name=None, arguments=': "ok"}'))
            ], content=None))]),
        ]
        client = FakeOpenAIClient([AsyncChunkStream(chunks)])
        model = ModelClient(client=client, model="test-model")

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "lookup"}],
            tool_specs=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            stream=True,
        )

        self.assertEqual(reply_type, ReplyType.TOOL_CALL)
        self.assertEqual(payload, [ChatToolCall(call_id="call-1", name="lookup", arguments='{"value": "ok"}')])


class AgentChatFlowTests(unittest.IsolatedAsyncioTestCase):
    def _build_agent(self, storage, model_client, tool_executor=None, tools=None):
        agent = Agent.__new__(Agent)
        agent.output_type = None
        agent.name = "test"
        agent.system_prompt = ""
        agent._assistant_sender_id = "agent:test"
        agent._memory_tools_enabled = True
        agent._private_handler = None
        agent.tool_manager = FakeToolManager(tools=tools)
        agent.model_client = model_client
        agent.message_storage = storage
        agent.message_handler = MessageHandler(message_storage=storage, system_prompt="")
        agent.memory_handler = FakeMemoryHandler()
        agent.tool_executor = tool_executor or FakeToolExecutor()
        return agent

    async def test_chat_sends_single_transcript_message_before_loop(self):
        storage = InMemoryMessageStorage([
            Message.create("Hello", role=RoleType.USER, sender_id="alice"),
            Message.create("Hi Alice", role=RoleType.ASSISTANT, sender_id="agent:test"),
        ])
        model_client = CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "Final answer"),
        ])
        agent = self._build_agent(storage=storage, model_client=model_client)

        result = await Agent.chat(
            agent,
            user_message="What should we do next?",
            user_id="bob",
            history_count=10,
            max_iter=2,
            enable_memory=False,
        )

        self.assertEqual(result, "Final answer")
        self.assertEqual(len(model_client.calls), 1)
        first_call_messages = model_client.calls[0]
        # No system message in input — instructions are passed separately
        self.assertEqual(first_call_messages[0]["role"], "user")
        self.assertIn("[speaker=alice]", first_call_messages[0]["content"])
        self.assertIn("[speaker=you]", first_call_messages[0]["content"])
        self.assertIn("[speaker=bob]", first_call_messages[0]["content"])
        # Instructions passed separately
        self.assertIsNotNone(model_client.instructions_calls[0])
        self.assertIn("Core Rules", model_client.instructions_calls[0])
        self.assertEqual(
            [message.role for message in storage.messages],
            [RoleType.USER, RoleType.ASSISTANT, RoleType.USER, RoleType.ASSISTANT],
        )

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


if __name__ == "__main__":
    unittest.main()
