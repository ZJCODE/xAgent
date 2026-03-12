import asyncio
import importlib
import sys
import unittest
import warnings
from tempfile import TemporaryDirectory

from xagent.components.message import MessageStorageLocal
from xagent.components.message.local_messages import MessageStorageLocalConfig
from xagent.interfaces.base import BaseAgentConfig, BaseAgentRunner
from xagent.schemas import Message, MessageType, RoleType, ToolCall


class MessageStorageLocalTests(unittest.TestCase):
    def test_local_storage_persists_and_isolates_sessions(self):
        with self.subTest("local_persistence"):
            with TemporaryDirectory() as temp_dir:
                db_path = f"{temp_dir}/messages.sqlite3"
                storage = MessageStorageLocal(path=db_path)

                primary_messages = [
                    Message.create("hello", role=RoleType.USER),
                    Message.create("world", role=RoleType.ASSISTANT, image_source="https://example.com/image.png"),
                    Message.create("x" * 5000, role=RoleType.USER),
                ]
                other_session_message = Message.create("other-session", role=RoleType.USER)
                other_user_message = Message.create("other-user", role=RoleType.USER)

                async def run():
                    await storage.add_messages("user-1", "session-a", primary_messages)
                    await storage.add_messages("user-1", "session-b", other_session_message)
                    await storage.add_messages("user-2", "session-a", other_user_message)

                    reloaded = MessageStorageLocal(path=db_path)
                    messages = await reloaded.get_messages("user-1", "session-a", count=10)
                    other_session = await reloaded.get_messages("user-1", "session-b", count=10)
                    other_user = await reloaded.get_messages("user-2", "session-a", count=10)

                    self.assertEqual([msg.content for msg in messages], ["hello", "world", "x" * 5000])
                    self.assertIsNotNone(messages[1].multimodal)
                    self.assertEqual(other_session[0].content, "other-session")
                    self.assertEqual(other_user[0].content, "other-user")
                    self.assertEqual(await reloaded.get_message_count("user-1", "session-a"), 3)

                    await reloaded.clear_history("user-1", "session-b")
                    self.assertEqual(await reloaded.get_messages("user-1", "session-b", count=10), [])

                asyncio.run(run())

    def test_local_pop_message_skips_tool_messages(self):
        with TemporaryDirectory() as temp_dir:
            storage = MessageStorageLocal(path=f"{temp_dir}/messages.sqlite3")
            user_message = Message.create("persist-me", role=RoleType.USER)
            tool_call_message = Message(
                type=MessageType.FUNCTION_CALL,
                role=RoleType.ASSISTANT,
                content="tool-call",
                tool_call=ToolCall(call_id="call-1", name="demo", arguments="{}"),
            )
            tool_output_message = Message(
                type=MessageType.FUNCTION_CALL_OUTPUT,
                role=RoleType.TOOL,
                content="tool-output",
                tool_call=ToolCall(call_id="call-1", output="ok"),
            )

            async def run():
                await storage.add_messages(
                    "user-1",
                    "session-a",
                    [user_message, tool_call_message, tool_output_message],
                )
                popped = await storage.pop_message("user-1", "session-a")
                remaining = await storage.get_messages("user-1", "session-a", count=10)

                self.assertIsNotNone(popped)
                self.assertEqual(popped.content, "persist-me")
                self.assertEqual(remaining, [])

            asyncio.run(run())

    def test_local_storage_and_lazy_imports_optional_modules(self):
        for module_name in [
            "xagent.components.message.cloud_messages",
            "xagent.components.memory.cloud_memory",
            "xagent.components.memory.message_buffer.redis_message_buffer",
            "xagent.components.memory.vector_store.upstach_vector_store",
        ]:
            sys.modules.pop(module_name, None)

        components_pkg = importlib.import_module("xagent.components")
        message_pkg = importlib.import_module("xagent.components.message")
        importlib.reload(components_pkg)
        importlib.reload(message_pkg)

        self.assertNotIn("xagent.components.message.cloud_messages", sys.modules)
        self.assertEqual(BaseAgentConfig.DEFAULT_STORAGE_MODE, "local")

        runner = BaseAgentRunner.__new__(BaseAgentRunner)
        runner.config = {"agent": {"storage_mode": "local"}}

        with TemporaryDirectory() as temp_dir:
            original_path = MessageStorageLocalConfig.DEFAULT_PATH
            MessageStorageLocalConfig.DEFAULT_PATH = f"{temp_dir}/messages.sqlite3"
            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    storage = BaseAgentRunner._initialize_message_storage(runner)
            finally:
                MessageStorageLocalConfig.DEFAULT_PATH = original_path

        self.assertIsInstance(storage, MessageStorageLocal)
        self.assertEqual(len(caught), 0)
        self.assertNotIn("xagent.components.message.cloud_messages", sys.modules)
