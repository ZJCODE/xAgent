import asyncio
import contextvars
import unittest
from types import SimpleNamespace

from pydantic import BaseModel

from xagent.components.message import MessageStorageBase
from xagent.core.agent import Agent
from xagent.core.config import AgentConfig, MemoryMode, ReplyType
from xagent.core.handlers.model import ChatToolCall, ModelClient, ModelErrorEvent, ModelStreamEvent
from xagent.core.handlers.message import MessageHandler
from xagent.core.providers import MODEL_API_ANTHROPIC_MESSAGES, MODEL_API_OPENAI_RESPONSES
from xagent.core.tools.executor import ToolDisplayResult, ToolExecutor
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


class CapturingStreamingModelClient(CapturingModelClient):
    def __init__(self, stream_turns):
        super().__init__(responses=[])
        self.stream_turns = list(stream_turns)
        self.stream_calls = []

    async def model_turn_events(self, messages, tool_specs, instructions=None, stream=False):
        self.calls.append(messages)
        self.instructions_calls.append(instructions)
        self.stream_calls.append(stream)
        for event in self.stream_turns.pop(0):
            yield event


class PausingStreamingModelClient(CapturingModelClient):
    def __init__(self, release_event):
        super().__init__(responses=[])
        self.release_event = release_event
        self.stream_calls = []

    async def model_turn_events(self, messages, tool_specs, instructions=None, stream=False):
        self.calls.append(messages)
        self.instructions_calls.append(instructions)
        self.stream_calls.append(stream)
        yield ModelStreamEvent(type="delta", delta="Hel")
        await self.release_event.wait()
        yield ModelStreamEvent(type="delta", delta="lo")


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


class FakeAttachmentToolExecutor:
    def __init__(self, display_result):
        self.display_result = display_result

    async def handle_tool_calls(self, tool_calls, input_messages, max_concurrent_tools):
        input_messages.extend([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "attach_artifact", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": self.display_result.description},
        ])
        return self.display_result


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


class FakeResponses:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeOpenAIClient:
    def __init__(self, responses=None, response_api_responses=None):
        responses = responses or []
        self.chat_completions = FakeChatCompletions(responses)
        self.chat = SimpleNamespace(completions=self.chat_completions)
        self.responses_api = FakeResponses(response_api_responses or [])
        self.responses = self.responses_api


class FakeAnthropicMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeAnthropicClient:
    def __init__(self, responses):
        self.messages = FakeAnthropicMessages(responses)


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


def _anthropic_response(content):
    return SimpleNamespace(content=content, stop_reason="end_turn")


def _responses_response(output_text=None, output=None):
    return SimpleNamespace(output_text=output_text, output=output or [])


class StructuredAnswer(BaseModel):
    answer: str


class ModelClientResponseTests(unittest.IsolatedAsyncioTestCase):
    def test_non_stream_response_preserves_text_on_tool_calls(self):
        raw_tool_call = SimpleNamespace(
            id="call-1",
            type="function",
            function=SimpleNamespace(name="lookup", arguments="{}"),
        )
        response = _chat_response(content="I will look that up first.", tool_calls=[raw_tool_call])

        reply_type, payload = ModelClient._handle_non_stream(response)

        self.assertEqual(reply_type, ReplyType.TOOL_CALL)
        self.assertEqual(payload[0].call_id, "call-1")
        self.assertEqual(payload[0].name, "lookup")
        self.assertEqual(payload[0].arguments, "{}")
        self.assertEqual(payload[0].assistant_content, "I will look that up first.")

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

    async def test_model_turn_events_non_stream_preserves_text_and_tool_calls(self):
        raw_tool_call = SimpleNamespace(
            id="call-1",
            type="function",
            function=SimpleNamespace(name="lookup", arguments="{}"),
        )
        client = FakeOpenAIClient([
            _chat_response(content="I will look that up first.", tool_calls=[raw_tool_call])
        ])
        model = ModelClient(client=client, model="test-model")

        events = [
            event async for event in model.model_turn_events(
                messages=[{"role": "user", "content": "lookup"}],
                tool_specs=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
                stream=False,
            )
        ]

        self.assertEqual([event.type for event in events], ["text", "tool_calls"])
        self.assertEqual(events[0].delta, "I will look that up first.")
        self.assertEqual(events[1].tool_calls[0].assistant_content, "I will look that up first.")
        self.assertFalse(client.chat_completions.calls[0]["stream"])

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

    async def test_call_uses_responses_api_when_selected(self):
        client = FakeOpenAIClient(response_api_responses=[_responses_response(output_text="ok")])
        model = ModelClient(
            client=client,
            model="test-model",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "hello"}],
            tool_specs=[{
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up data",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            instructions=[{"role": "system", "name": "core", "content": "Core Rules"}],
        )

        self.assertEqual(reply_type, ReplyType.SIMPLE_REPLY)
        self.assertEqual(payload, "ok")
        call = client.responses_api.calls[0]
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["instructions"], "Core Rules")
        self.assertEqual(call["input"], [{"role": "user", "content": "hello"}])
        self.assertFalse(call["store"])
        self.assertEqual(call["tool_choice"], "auto")
        self.assertEqual(call["include"], ["reasoning.encrypted_content"])
        self.assertEqual(
            call["tools"],
            [{
                "type": "function",
                "name": "lookup",
                "description": "Look up data",
                "parameters": {"type": "object", "properties": {}},
                "strict": False,
            }],
        )

    async def test_responses_structured_output_uses_text_format(self):
        client = FakeOpenAIClient(response_api_responses=[
            _responses_response(output_text='{"answer": "ok"}')
        ])
        model = ModelClient(
            client=client,
            model="test-model",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "answer as json"}],
            tool_specs=None,
            instructions="Return JSON",
            output_type=StructuredAnswer,
        )

        self.assertEqual(reply_type, ReplyType.STRUCTURED_REPLY)
        self.assertEqual(payload.answer, "ok")
        call = client.responses_api.calls[0]
        self.assertEqual(call["text"]["format"]["type"], "json_schema")
        self.assertEqual(call["text"]["format"]["name"], "StructuredAnswer")
        self.assertFalse(call["text"]["format"]["strict"])
        self.assertIn("JSON schema", call["instructions"])

    async def test_responses_tool_call_preserves_replay_items(self):
        response_items = [
            {"type": "reasoning", "encrypted_content": "encrypted"},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "lookup",
                "arguments": '{"query": "x"}',
            },
        ]
        client = FakeOpenAIClient(response_api_responses=[
            _responses_response(output=response_items)
        ])
        model = ModelClient(
            client=client,
            model="test-model",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "lookup"}],
            tool_specs=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        )

        self.assertEqual(reply_type, ReplyType.TOOL_CALL)
        self.assertEqual(payload[0].call_id, "call-1")
        self.assertEqual(payload[0].name, "lookup")
        self.assertEqual(payload[0].arguments, '{"query": "x"}')
        self.assertEqual(payload[0].response_items, response_items)

    async def test_call_uses_anthropic_messages_backend(self):
        client = FakeAnthropicClient([
            _anthropic_response([{"type": "text", "text": "ok"}])
        ])
        model = ModelClient(
            client=client,
            model="MiniMax-M2.7",
            model_api=MODEL_API_ANTHROPIC_MESSAGES,
            max_tokens=1234,
        )

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "hello"}],
            tool_specs=[{
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up data",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            instructions="Core Rules",
        )

        self.assertEqual(reply_type, ReplyType.SIMPLE_REPLY)
        self.assertEqual(payload, "ok")
        call = client.messages.calls[0]
        self.assertEqual(call["model"], "MiniMax-M2.7")
        self.assertEqual(call["max_tokens"], 1234)
        self.assertEqual(call["system"], "Core Rules")
        self.assertEqual(call["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(
            call["tools"],
            [{
                "name": "lookup",
                "description": "Look up data",
                "input_schema": {"type": "object", "properties": {}},
            }],
        )
        self.assertEqual(call["tool_choice"], {"type": "auto"})

    async def test_anthropic_tool_call_preserves_content_blocks_for_next_turn(self):
        response_blocks = [
            {"type": "thinking", "thinking": "Need a lookup.", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"query": "x"}},
        ]
        client = FakeAnthropicClient([_anthropic_response(response_blocks)])
        model = ModelClient(client=client, model="MiniMax-M2.7", model_api=MODEL_API_ANTHROPIC_MESSAGES)

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "hello"}],
            tool_specs=[{
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }],
        )

        self.assertEqual(reply_type, ReplyType.TOOL_CALL)
        self.assertEqual(payload[0].call_id, "toolu_1")
        self.assertEqual(payload[0].name, "lookup")
        self.assertEqual(payload[0].arguments, '{"query": "x"}')
        self.assertEqual(payload[0].content_blocks, response_blocks)

    async def test_anthropic_request_replays_assistant_tool_content_blocks(self):
        content_blocks = [
            {"type": "thinking", "thinking": "Need a lookup.", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"query": "x"}},
        ]
        client = FakeAnthropicClient([
            _anthropic_response([{"type": "text", "text": "done"}])
        ])
        model = ModelClient(client=client, model="MiniMax-M2.7", model_api=MODEL_API_ANTHROPIC_MESSAGES)

        await model.call(
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "toolu_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"query": "x"}'},
                    }],
                    "content_blocks": content_blocks,
                },
                {"role": "tool", "tool_call_id": "toolu_1", "content": "lookup result"},
            ],
            tool_specs=None,
        )

        call = client.messages.calls[0]
        self.assertEqual(call["messages"][0], {"role": "assistant", "content": content_blocks})
        self.assertEqual(
            call["messages"][1],
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "lookup result",
                }],
            },
        )

    async def test_call_strips_top_level_message_names(self):
        client = FakeOpenAIClient([_chat_response(content="ok")])
        model = ModelClient(client=client, model="test-model")
        instruction_messages = [
            {
                "role": "system",
                "name": AgentConfig.CORE_INTERACTION_RULES_NAME,
                "content": "Core Rules",
            },
            {
                "role": "system",
                "name": AgentConfig.TOOL_POLICY_NAME,
                "content": "<tool_policy>Policy</tool_policy>",
            },
        ]
        user_messages = [
            {
                "role": "user",
                "name": AgentConfig.CURRENT_TASK_NAME,
                "content": "<current_task>Task</current_task>",
            }
        ]

        reply_type, payload = await model.call(
            messages=user_messages,
            tool_specs=None,
            instructions=instruction_messages,
        )

        self.assertEqual(reply_type, ReplyType.SIMPLE_REPLY)
        self.assertEqual(payload, "ok")
        call = client.chat_completions.calls[0]
        self.assertEqual(
            call["messages"],
            [
                {"role": "system", "content": "Core Rules\n\n<tool_policy>Policy</tool_policy>"},
                {"role": "user", "content": "<current_task>Task</current_task>"},
            ],
        )
        self.assertEqual(instruction_messages[0]["name"], AgentConfig.CORE_INTERACTION_RULES_NAME)
        self.assertEqual(user_messages[0]["name"], AgentConfig.CURRENT_TASK_NAME)

    async def test_call_coalesces_system_layers_for_multimodal_chat_messages(self):
        client = FakeOpenAIClient([_chat_response(content="ok")])
        model = ModelClient(client=client, model="test-model")

        reply_type, payload = await model.call(
            messages=[{
                "role": "user",
                "name": AgentConfig.CURRENT_TASK_NAME,
                "content": [
                    {"type": "text", "text": "Inspect this image"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/chart.png"}},
                ],
            }],
            tool_specs=None,
            instructions=[
                {
                    "role": "system",
                    "name": AgentConfig.CORE_INTERACTION_RULES_NAME,
                    "content": "Core Rules",
                },
                {
                    "role": "system",
                    "name": AgentConfig.TOOL_POLICY_NAME,
                    "content": "<tool_policy>Policy</tool_policy>",
                },
            ],
        )

        self.assertEqual(reply_type, ReplyType.SIMPLE_REPLY)
        self.assertEqual(payload, "ok")
        call = client.chat_completions.calls[0]
        self.assertEqual(
            call["messages"],
            [
                {"role": "system", "content": "Core Rules\n\n<tool_policy>Policy</tool_policy>"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Inspect this image"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/chart.png"}},
                    ],
                },
            ],
        )
        self.assertTrue(all("name" not in message for message in call["messages"]))

    async def test_call_strips_structured_output_message_name(self):
        client = FakeOpenAIClient([_chat_response(content='{"answer": "ok"}')])
        model = ModelClient(client=client, model="test-model")

        reply_type, payload = await model.call(
            messages=[{
                "role": "user",
                "name": AgentConfig.CURRENT_TASK_NAME,
                "content": "answer as json",
            }],
            tool_specs=None,
            instructions=[{
                "role": "system",
                "name": AgentConfig.CORE_INTERACTION_RULES_NAME,
                "content": "Return JSON",
            }],
            output_type=StructuredAnswer,
        )

        self.assertEqual(reply_type, ReplyType.STRUCTURED_REPLY)
        self.assertEqual(payload.answer, "ok")
        call = client.chat_completions.calls[0]
        self.assertTrue(all("name" not in message for message in call["messages"]))
        self.assertIn("JSON schema", call["messages"][0]["content"])

    def test_strip_message_names_preserves_tool_call_function_names(self):
        messages = [{
            "role": "assistant",
            "name": "assistant_layer",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        }]

        stripped = ModelClient._strip_message_names(messages)

        self.assertNotIn("name", stripped[0])
        self.assertEqual(stripped[0]["tool_calls"][0]["function"]["name"], "lookup")

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

    async def test_responses_stream_text_yields_chunks_and_stores_final_text(self):
        events = [
            SimpleNamespace(type="response.output_text.delta", delta="Hel"),
            SimpleNamespace(type="response.output_text.delta", delta="lo"),
        ]
        client = FakeOpenAIClient(response_api_responses=[AsyncChunkStream(events)])
        model = ModelClient(
            client=client,
            model="test-model",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )
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

    async def test_stream_text_before_tool_call_is_preserved(self):
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content="I will check.",
                tool_calls=None,
                reasoning_content=None,
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=[
                SimpleNamespace(index=0, id="call-1", type="function", function=SimpleNamespace(name="lookup", arguments="{}"))
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
        self.assertEqual(payload[0].call_id, "call-1")
        self.assertEqual(payload[0].assistant_content, "I will check.")

    async def test_responses_stream_tool_calls_accumulates_split_arguments(self):
        events = [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=1,
                item=SimpleNamespace(
                    type="function_call",
                    id="fc_1",
                    call_id="call-1",
                    name="lookup",
                    arguments="",
                ),
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                output_index=1,
                item_id="fc_1",
                delta='{"value"',
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                output_index=1,
                item_id="fc_1",
                delta=': "ok"}',
            ),
        ]
        client = FakeOpenAIClient(response_api_responses=[AsyncChunkStream(events)])
        model = ModelClient(
            client=client,
            model="test-model",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "lookup"}],
            tool_specs=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            stream=True,
        )

        self.assertEqual(reply_type, ReplyType.TOOL_CALL)
        self.assertEqual(payload[0].call_id, "call-1")
        self.assertEqual(payload[0].name, "lookup")
        self.assertEqual(payload[0].arguments, '{"value": "ok"}')
        self.assertEqual(payload[0].response_items[-1]["type"], "function_call")

    async def test_responses_stream_text_before_tool_call_is_preserved_in_replay(self):
        events = [
            SimpleNamespace(type="response.output_text.delta", delta="I will check."),
            SimpleNamespace(
                type="response.output_item.added",
                output_index=1,
                item=SimpleNamespace(
                    type="function_call",
                    id="fc_1",
                    call_id="call-1",
                    name="lookup",
                    arguments="",
                ),
            ),
            SimpleNamespace(
                type="response.function_call_arguments.done",
                output_index=1,
                item=SimpleNamespace(
                    type="function_call",
                    id="fc_1",
                    call_id="call-1",
                    name="lookup",
                    arguments="{}",
                ),
            ),
        ]
        client = FakeOpenAIClient(response_api_responses=[AsyncChunkStream(events)])
        model = ModelClient(
            client=client,
            model="test-model",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "lookup"}],
            tool_specs=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            stream=True,
        )

        self.assertEqual(reply_type, ReplyType.TOOL_CALL)
        self.assertEqual(payload[0].assistant_content, "I will check.")
        self.assertEqual(payload[0].response_items[0]["type"], "message")
        self.assertEqual(payload[0].response_items[0]["content"][0]["text"], "I will check.")

    async def test_model_call_exception_returns_error_event(self):
        class FailingChatCompletions:
            async def create(self, **kwargs):
                raise RuntimeError("provider rejected messages")

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=FailingChatCompletions())
        )
        model = ModelClient(client=client, model="test-model")

        reply_type, payload = await model.call(
            messages=[{"role": "user", "content": "hello"}],
            tool_specs=None,
        )

        self.assertEqual(reply_type, ReplyType.ERROR)
        self.assertIsInstance(payload, ModelErrorEvent)
        self.assertEqual(payload.code, "model_call_failed")
        self.assertEqual(payload.message, "Model call failed.")
        self.assertIn("provider rejected messages", payload.details)

    def test_structured_validation_error_returns_error_event(self):
        response = _chat_response(content='{"wrong": "shape"}')

        reply_type, payload = ModelClient._handle_non_stream(
            response,
            output_type=StructuredAnswer,
        )

        self.assertEqual(reply_type, ReplyType.ERROR)
        self.assertIsInstance(payload, ModelErrorEvent)
        self.assertEqual(payload.code, "structured_output_validation_failed")


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
        agent.observability = observability or NoopObservabilityRuntime()
        agent.tool_manager = FakeToolManager(tools=tools)
        agent.model_client = model_client
        agent.message_storage = storage
        agent.message_handler = MessageHandler(message_storage=storage, system_prompt="")
        agent.memory_handler = memory_handler or FakeMemoryHandler()
        agent.tool_executor = tool_executor or FakeToolExecutor()
        return agent

    def test_reset_memory_mode_ignores_token_from_different_context(self):
        agent = self._build_agent(
            storage=InMemoryMessageStorage(),
            model_client=CapturingModelClient([]),
        )
        memory_mode_var = agent._get_memory_mode_var()
        other_context = contextvars.Context()
        token = other_context.run(memory_mode_var.set, MemoryMode.DISABLED)

        agent._reset_memory_mode(token)

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
        self.assertEqual(
            [message["name"] for message in first_call_messages],
            [AgentConfig.RECENT_EXPERIENCE_NAME, AgentConfig.CURRENT_TASK_NAME],
        )
        self.assertEqual(model_client.instructions_calls[0][0]["name"], AgentConfig.CORE_INTERACTION_RULES_NAME)
        self.assertEqual(second_call_messages[2]["role"], "assistant")
        self.assertEqual(second_call_messages[2]["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(second_call_messages[3]["role"], "tool")
        self.assertEqual(second_call_messages[3]["tool_call_id"], "call-1")
        self.assertEqual(
            [message.role for message in storage.messages],
            [RoleType.USER, RoleType.ASSISTANT],
        )

    async def test_chat_rejects_image_input_for_non_vision_provider(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([(ReplyType.SIMPLE_REPLY, "should not be called")])
        agent = self._build_agent(storage=storage, model_client=model_client)
        agent.supports_vision = False

        result = await Agent.chat(
            agent,
            user_message="Please inspect this image",
            user_id="alice",
            image_source="data:image/png;base64,AAAA",
            enable_memory=False,
        )

        self.assertIn("does not support image input", result)
        self.assertEqual(model_client.calls, [])
        stored_messages = await storage.get_messages(10)
        self.assertEqual(len(stored_messages), 2)
        self.assertIsNone(stored_messages[0].multimodal)
        self.assertIn("does not support image input", stored_messages[1].content)

    async def test_chat_events_streams_preface_then_tool_then_final_reply(self):
        storage = InMemoryMessageStorage()
        memory_handler = FakeMemoryHandler()
        model_client = CapturingStreamingModelClient([
            [
                ModelStreamEvent(type="delta", delta="I will check."),
                ModelStreamEvent(type="tool_calls", tool_calls=[
                    ChatToolCall(
                        call_id="call-1",
                        name="lookup",
                        arguments="{}",
                        assistant_content="I will check.",
                    )
                ]),
            ],
            [
                ModelStreamEvent(type="delta", delta="We are in /tmp."),
            ],
        ])
        tool_executor = FakeToolExecutor()
        agent = self._build_agent(
            storage=storage,
            model_client=model_client,
            tool_executor=tool_executor,
            memory_handler=memory_handler,
        )

        events = [
            event async for event in Agent.chat_events(
                agent,
                user_message="Where are we?",
                user_id="bob",
                history_count=10,
                max_iter=3,
                stream=True,
                enable_memory=True,
            )
        ]

        self.assertEqual(
            [event["type"] for event in events],
            [
                "message_start",
                "message_delta",
                "message_done",
                "tool_call",
                "tool_result",
                "message_start",
                "message_delta",
                "message_done",
                "done",
            ],
        )
        self.assertEqual(events[2]["phase"], "preface")
        self.assertEqual(events[2]["content"], "I will check.")
        self.assertEqual(events[3]["name"], "lookup")
        self.assertEqual(events[7]["phase"], "final")
        self.assertEqual(events[7]["content"], "We are in /tmp.")
        self.assertEqual([message.role for message in storage.messages], [
            RoleType.USER,
            RoleType.ASSISTANT,
            RoleType.ASSISTANT,
        ])
        self.assertEqual(storage.messages[1].metadata["turn_phase"], "preface")
        self.assertEqual(storage.messages[2].metadata["turn_phase"], "final")
        self.assertEqual(memory_handler.experience_messages, [storage.messages[0], storage.messages[2]])
        self.assertEqual(model_client.stream_calls, [True, True])

    async def test_chat_events_emits_delta_before_model_turn_finishes(self):
        storage = InMemoryMessageStorage()
        release_event = asyncio.Event()
        model_client = PausingStreamingModelClient(release_event)
        agent = self._build_agent(
            storage=storage,
            model_client=model_client,
            memory_handler=FakeMemoryHandler(),
        )

        events = Agent.chat_events(
            agent,
            user_message="Stream this",
            user_id="bob",
            stream=True,
            enable_memory=False,
        ).__aiter__()

        first_event = await asyncio.wait_for(events.__anext__(), timeout=0.2)
        second_event = await asyncio.wait_for(events.__anext__(), timeout=0.2)

        self.assertEqual(first_event["type"], "message_start")
        self.assertEqual(second_event["type"], "message_delta")
        self.assertEqual(second_event["delta"], "Hel")
        self.assertFalse(release_event.is_set())

        release_event.set()
        remaining_events = [event async for event in events]

        self.assertEqual(remaining_events[0]["type"], "message_delta")
        self.assertEqual(remaining_events[0]["delta"], "lo")
        self.assertEqual(remaining_events[1]["type"], "message_done")
        self.assertEqual(remaining_events[1]["phase"], "final")
        self.assertEqual(remaining_events[1]["content"], "Hello")
        self.assertEqual(remaining_events[2], {"type": "done"})
        self.assertEqual(model_client.stream_calls, [True])

    async def test_chat_stream_true_returns_live_text_generator(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingStreamingModelClient([
            [
                ModelStreamEvent(type="delta", delta="Hel"),
                ModelStreamEvent(type="delta", delta="lo"),
            ],
        ])
        agent = self._build_agent(
            storage=storage,
            model_client=model_client,
            memory_handler=FakeMemoryHandler(),
        )

        response = await Agent.chat(
            agent,
            user_message="Stream this",
            user_id="bob",
            stream=True,
            enable_memory=False,
        )

        collected = [chunk async for chunk in response]
        self.assertEqual(collected, ["Hel", "lo"])
        self.assertEqual(model_client.stream_calls, [True])
        self.assertEqual(storage.messages[-1].content, "Hello")

    async def test_chat_events_without_stream_emits_done_boundaries_only(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingStreamingModelClient([
            [
                ModelStreamEvent(type="text", delta="I will check."),
                ModelStreamEvent(type="tool_calls", tool_calls=[
                    ChatToolCall(call_id="call-1", name="lookup", arguments="{}")
                ]),
            ],
            [
                ModelStreamEvent(type="text", delta="We are in /tmp."),
            ],
        ])
        agent = self._build_agent(
            storage=storage,
            model_client=model_client,
            tool_executor=FakeToolExecutor(),
            memory_handler=FakeMemoryHandler(),
        )

        events = [
            event async for event in Agent.chat_events(
                agent,
                user_message="Where are we?",
                user_id="bob",
                stream=False,
                enable_memory=True,
            )
        ]

        self.assertNotIn("message_delta", [event["type"] for event in events])
        self.assertEqual(
            [(event["type"], event.get("phase"), event.get("content")) for event in events if event["type"] == "message_done"],
            [
                ("message_done", "preface", "I will check."),
                ("message_done", "final", "We are in /tmp."),
            ],
        )
        self.assertEqual(model_client.stream_calls, [False, False])

    async def test_chat_events_includes_tool_attachments_on_done_event(self):
        storage = InMemoryMessageStorage()
        attachment = {
            "kind": "image",
            "path": "temp/images/result.png",
            "blob_url": "/api/workspace/blob?path=temp%2Fimages%2Fresult.png",
            "mime_type": "image/png",
            "file_name": "result.png",
            "caption": "Processed",
        }
        tool_result = ToolDisplayResult(
            content="![Processed](/api/workspace/blob?path=temp%2Fimages%2Fresult.png)",
            description="[Artifact attached by tool `attach_artifact` and displayed to user.]",
            attachments=[attachment],
        )
        model_client = CapturingStreamingModelClient([
            [
                ModelStreamEvent(type="tool_calls", tool_calls=[
                    ChatToolCall(call_id="call-1", name="attach_artifact", arguments="{}")
                ]),
            ],
        ])
        agent = self._build_agent(
            storage=storage,
            model_client=model_client,
            tool_executor=FakeAttachmentToolExecutor(tool_result),
            memory_handler=FakeMemoryHandler(),
        )

        events = [
            event async for event in Agent.chat_events(
                agent,
                user_message="send the image",
                user_id="bob",
                stream=False,
                enable_memory=True,
            )
        ]

        done_events = [event for event in events if event["type"] == "message_done"]
        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0]["content"], tool_result.content)
        self.assertEqual(done_events[0]["attachments"], [attachment])
        self.assertEqual(storage.messages[-1].content, tool_result.description)

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
        )

        self.assertEqual(result, "Traced answer")
        self.assertTrue(observability.entered)
        self.assertTrue(observability.exited)
        self.assertEqual(observability.turn_kwargs["user_id"], "alice")
        self.assertEqual(observability.turn_kwargs["model"], AgentConfig.DEFAULT_MODEL)
        self.assertEqual(observability.turn_kwargs["memory_mode"], "full")
        self.assertFalse(observability.turn_kwargs["stream"])

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
        self.assertEqual(storage.last_count, AgentConfig.DEFAULT_HISTORY_COUNT)
        transcript = next(
            message for message in model_client.calls[0]
            if message["name"] == AgentConfig.RECENT_EXPERIENCE_NAME
        )["content"]
        self.assertNotIn("old-00", transcript)
        self.assertNotIn("old-10", transcript)
        self.assertIn("old-49", transcript)
        self.assertIn("latest request", transcript)

    async def test_chat_hides_model_error_event_from_user(self):
        storage = InMemoryMessageStorage()
        model_client = CapturingModelClient([
            (
                ReplyType.ERROR,
                ModelErrorEvent(
                    code="model_call_failed",
                    message="Model call failed.",
                    details="provider rejected messages",
                ),
            ),
        ])
        agent = self._build_agent(storage=storage, model_client=model_client)

        result = await Agent.chat(
            agent,
            user_message="hello",
            user_id="alice",
            max_iter=1,
        )

        self.assertEqual(result, "Sorry, I encountered an error while processing your request.")
        stored_messages = await storage.get_messages(10)
        self.assertEqual(len(stored_messages), 1)
        self.assertEqual(stored_messages[0].content, "hello")

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

    async def test_transcript_budget_records_images_without_attaching_them(self):
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

        self.assertIsInstance(model_message["content"], str)
        self.assertIn("[Attached image: 1]", model_message["content"])

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

        tool_message, display_result = await executor.execute_single(
            FakeToolCall(name="lookup", arguments='{"value": "ok"}')
        )

        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "call-1")
        self.assertEqual(tool_message["content"], '{"value": "ok"}')
        self.assertIsNone(display_result)
        self.assertEqual(storage.messages, [])

    async def test_generated_image_tool_result_exposes_structured_attachment(self):
        async def draw() -> dict:
            return {
                "status": "ok",
                "type": "generated_image",
                "prompt": "chart",
                "image": {
                    "path": "temp/images/chart.png",
                    "blob_url": "/api/workspace/blob?path=temp%2Fimages%2Fchart.png",
                    "markdown": "![Generated image](/api/workspace/blob?path=temp%2Fimages%2Fchart.png)",
                    "mime_type": "image/png",
                },
            }

        executor = ToolExecutor(
            tool_manager=FakeToolManager(tools={"draw": draw}),
            message_storage=InMemoryMessageStorage(),
            client=None,
        )

        tool_message, display_result = await executor.execute_single(FakeToolCall(name="draw"))

        self.assertIn("Image generated by tool `draw`", tool_message["content"])
        self.assertIsNotNone(display_result)
        self.assertEqual(display_result.content, "![Generated image](/api/workspace/blob?path=temp%2Fimages%2Fchart.png)")
        self.assertEqual(display_result.attachments[0]["kind"], "image")
        self.assertEqual(display_result.attachments[0]["path"], "temp/images/chart.png")

    async def test_artifact_tool_result_exposes_structured_attachment(self):
        async def attach() -> dict:
            return {
                "status": "ok",
                "type": "artifact_attachment",
                "artifact": {
                    "kind": "file",
                    "path": "reports/out.pdf",
                    "blob_url": "/api/workspace/blob?path=reports%2Fout.pdf",
                    "mime_type": "application/pdf",
                    "file_name": "out.pdf",
                    "caption": "Report",
                },
            }

        executor = ToolExecutor(
            tool_manager=FakeToolManager(tools={"attach_artifact": attach}),
            message_storage=InMemoryMessageStorage(),
            client=None,
        )

        tool_message, display_result = await executor.execute_single(FakeToolCall(name="attach_artifact"))

        self.assertIn("Artifact attached by tool `attach_artifact`", tool_message["content"])
        self.assertIsNotNone(display_result)
        self.assertEqual(display_result.content, "[Report](/api/workspace/blob?path=reports%2Fout.pdf)")
        self.assertEqual(display_result.attachments[0]["kind"], "file")
        self.assertEqual(display_result.attachments[0]["caption"], "Report")

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

    async def test_handle_tool_calls_appends_responses_replay_items_and_outputs(self):
        async def lookup() -> str:
            return "ok"

        response_items = [
            {"type": "reasoning", "encrypted_content": "encrypted"},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "lookup",
                "arguments": "{}",
            },
        ]
        storage = InMemoryMessageStorage()
        executor = ToolExecutor(
            tool_manager=FakeToolManager(tools={"lookup": lookup}),
            message_storage=storage,
            client=None,
        )
        messages = [{"role": "user", "content": "run tool"}]

        result = await executor.handle_tool_calls(
            [ChatToolCall(
                call_id="call-1",
                name="lookup",
                arguments="{}",
                response_items=response_items,
            )],
            messages,
            max_concurrent_tools=1,
        )

        self.assertIsNone(result)
        self.assertEqual(messages[1], response_items[0])
        self.assertEqual(messages[2], response_items[1])
        self.assertEqual(
            messages[3],
            {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
        )

    async def test_caption_image_can_use_responses_api(self):
        client = FakeOpenAIClient(response_api_responses=[
            _responses_response(output_text="A small generated chart.")
        ])
        executor = ToolExecutor(
            tool_manager=FakeToolManager(),
            message_storage=InMemoryMessageStorage(),
            client=client,
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        caption = await executor._caption_image("data:image/png;base64,abc", "draw a chart")

        self.assertEqual(caption, "A small generated chart.")
        call = client.responses_api.calls[0]
        self.assertFalse(call["store"])
        self.assertEqual(call["input"][0]["content"][1]["type"], "input_image")


if __name__ == "__main__":
    unittest.main()
