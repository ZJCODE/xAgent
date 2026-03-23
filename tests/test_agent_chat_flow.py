import unittest

from xagent.components.message.base_messages import MessageStorageBase
from xagent.core.agent import Agent
from xagent.core.config import ReplyType
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

    async def ensure_mcp_ready(self):
        return None

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
            {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-1", "output": "lookup result"},
        ])
        return None


class FakeToolCall:
    def __init__(self, name="lookup", arguments="{}", call_id="call-1"):
        self.type = "function_call"
        self.name = name
        self.arguments = arguments
        self.call_id = call_id


class AgentChatFlowTests(unittest.IsolatedAsyncioTestCase):
    def _build_agent(self, storage, model_client, tool_executor=None, tools=None):
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
        self.assertEqual(second_call_messages[1]["type"], "function_call")
        self.assertEqual(second_call_messages[2]["type"], "function_call_output")
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

        tool_messages, image_data, description = await executor.execute_single(
            FakeToolCall(name="lookup", arguments='{"value": "ok"}')
        )

        self.assertEqual(len(tool_messages), 2)
        self.assertIsNone(image_data)
        self.assertIsNone(description)
        self.assertEqual(storage.messages, [])


if __name__ == "__main__":
    unittest.main()