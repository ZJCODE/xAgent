import json
import unittest
from types import SimpleNamespace

from xagent.core.agent import Agent, ReplyType
from xagent.schemas import Message, MessageType, RoleType
from xagent.utils.tool_decorator import function_tool


class InMemoryMessageStorage:
    def __init__(self):
        self._messages = {}

    async def add_messages(self, user_id, session_id, messages, **kwargs):
        bucket = self._messages.setdefault((user_id, session_id), [])
        if isinstance(messages, list):
            bucket.extend(messages)
        else:
            bucket.append(messages)

    async def get_messages(self, user_id, session_id, count=20):
        return list(self._messages.get((user_id, session_id), []))[-count:]

    async def clear_history(self, user_id, session_id):
        self._messages.pop((user_id, session_id), None)

    async def pop_message(self, user_id, session_id):
        bucket = self._messages.get((user_id, session_id), [])
        return bucket.pop() if bucket else None


class DummyMemoryStorage:
    def __init__(self):
        self.llm_service = SimpleNamespace(
            should_preprocess_query=lambda query, pre_chat=None: False
        )

    async def add(self, user_id, messages):
        return None

    async def store(self, user_id, content):
        return ""

    async def retrieve(self, user_id, query, limit=5, query_context=None, enable_query_process=False):
        return []

    async def extract_meta(self, user_id, days=1):
        return []

    async def clear(self, user_id):
        return None

    async def delete(self, memory_ids):
        return None


class FakeAsyncStream:
    def __init__(self, events):
        self._events = list(events)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        return event


class DummyResponsesAPI:
    def __init__(self, create_responses):
        self._create_responses = list(create_responses)

    async def create(self, **kwargs):
        if not self._create_responses:
            raise AssertionError("No fake create response configured")
        response = self._create_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def parse(self, **kwargs):
        raise AssertionError("Structured output is not expected in this test")


def make_message_response(text: str):
    return SimpleNamespace(
        output_text=text,
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(text=text)],
            )
        ],
    )


def make_text_stream(text: str) -> FakeAsyncStream:
    response = make_message_response(text)
    return FakeAsyncStream(
        [
            SimpleNamespace(type="response.created"),
            SimpleNamespace(type="response.output_text.delta", delta=text, response=response),
            SimpleNamespace(type="response.completed", response=response),
        ]
    )


def make_tool_call_stream(name: str, arguments: dict, call_id: str = "call-1") -> FakeAsyncStream:
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                name=name,
                arguments=json.dumps(arguments),
                call_id=call_id,
            )
        ],
        output_text="",
    )
    return FakeAsyncStream(
        [
            SimpleNamespace(
                type="response.output_item.added",
                item=SimpleNamespace(type="function_call"),
            ),
            SimpleNamespace(type="response.completed", response=response),
        ]
    )


class AgentStreamingTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, create_responses, tools=None):
        storage = InMemoryMessageStorage()
        client = SimpleNamespace(responses=DummyResponsesAPI(create_responses))
        agent = Agent(
            name="StreamAgent",
            client=client,
            tools=tools or [],
            message_storage=storage,
            memory_storage=DummyMemoryStorage(),
        )
        return agent, storage

    async def collect_stream(self, stream):
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
        return "".join(chunks)

    async def test_streaming_text_reply_is_emitted_and_persisted(self):
        agent, storage = self.make_agent([make_text_stream("hello world")])

        stream = await agent.chat(
            user_message="hi",
            user_id="user-1",
            session_id="session-1",
            stream=True,
        )

        self.assertTrue(hasattr(stream, "__aiter__"))
        self.assertEqual(await self.collect_stream(stream), "hello world")

        stored_messages = await storage.get_messages(
            "user-1",
            agent.normalize_session_id("session-1"),
            count=10,
        )
        self.assertEqual([message.content for message in stored_messages], ["hi", "hello world"])

    async def test_streaming_tool_call_round_trip_continues_to_text_reply(self):
        @function_tool(
            name="echo_tool",
            description="Return a formatted echo string.",
            param_descriptions={"value": "Text to echo back."},
        )
        async def echo_tool(value: str) -> str:
            return f"echo:{value}"

        agent, storage = self.make_agent(
            [
                make_tool_call_stream("echo_tool", {"value": "payload"}),
                make_text_stream("tool complete"),
            ],
            tools=[echo_tool],
        )

        stream = await agent.chat(
            user_message="run the tool",
            user_id="user-1",
            session_id="session-2",
            stream=True,
        )

        self.assertEqual(await self.collect_stream(stream), "tool complete")

        stored_messages = await storage.get_messages(
            "user-1",
            agent.normalize_session_id("session-2"),
            count=10,
        )
        message_types = [message.type for message in stored_messages]
        self.assertIn(MessageType.FUNCTION_CALL, message_types)
        self.assertIn(MessageType.FUNCTION_CALL_OUTPUT, message_types)
        self.assertEqual(stored_messages[-1].content, "tool complete")

    async def test_streaming_empty_output_returns_error_generator(self):
        agent, _ = self.make_agent(
            [FakeAsyncStream([SimpleNamespace(type="response.completed", response=SimpleNamespace(output=[], output_text=""))])]
        )

        reply_type, response = await agent._call_model(
            input_msgs=[Message.create("hi", role=RoleType.USER).to_dict()],
            user_id="user-1",
            session_id="session-3",
            stream=True,
        )

        self.assertEqual(reply_type, ReplyType.ERROR)
        self.assertEqual(await self.collect_stream(response), "No valid output from model response.")

    async def test_streaming_model_error_is_exposed_as_stream(self):
        agent, _ = self.make_agent([RuntimeError("boom")])

        reply_type, response = await agent._call_model(
            input_msgs=[Message.create("hi", role=RoleType.USER).to_dict()],
            user_id="user-1",
            session_id="session-4",
            stream=True,
        )

        self.assertEqual(reply_type, ReplyType.ERROR)
        self.assertIn("boom", await self.collect_stream(response))
