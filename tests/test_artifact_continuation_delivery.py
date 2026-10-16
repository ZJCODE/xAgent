"""Consumers retain files when a tool delivery is followed by a text reply."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from tests import test_agent_chat_flow as chat_test
from xagent.integrations.api.adapter import ApiChannelAdapter
from xagent.integrations.api.chat_service import ChatService
from xagent.integrations.api.config import ChatLimits
from xagent.integrations.api.task_dispatch import TaskDispatchService
from xagent.integrations.weixin.adapter import WeixinAdapter


class ArtifactContinuationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.attachments = []
        for name in ("first.txt", "second.txt"):
            (self.root / name).write_text(name)
            self.attachments.append({"kind": "file", "path": name})

    async def events(self, **kwargs):
        yield {"type": "message_done", "phase": "final", "content": "", "attachments": self.attachments[:1]}
        yield {"type": "message_done", "phase": "final", "content": "Both reports are ready."}
        yield {"type": "message_done", "phase": "final", "content": "", "attachments": self.attachments}
        yield {"type": "done"}

    async def test_api_scheduled_result_keeps_text_and_all_attachments(self):
        agent = SimpleNamespace(chat_events=self.events)
        chat = ChatService(agent, contacts_file=self.root / "contacts.json", limits=ChatLimits())
        service = TaskDispatchService(agent, chat=chat, delivery=None)

        result = await service._scheduled_agent_event_result(
            self.events, prompt="Send reports", user_id="alice", channel="api", deadline=time.monotonic() + 5,
        )

        self.assertEqual(result.content, "Both reports are ready.")
        self.assertEqual([attachment["path"] for attachment in result.attachments], ["first.txt", "second.txt"])

    async def test_weixin_scheduled_result_keeps_text_and_all_attachments(self):
        adapter = WeixinAdapter.__new__(WeixinAdapter)
        adapter.agent = SimpleNamespace(workspace_dir=self.root, chat_events=self.events)
        task = SimpleNamespace(task_type="agent", content="Send reports", target={}, task_id="reports")

        result = await adapter._scheduled_task_result(task, user_id="alice")

        self.assertEqual(result.content, "Both reports are ready.")
        self.assertEqual([attachment.path.name for attachment in result.attachments], ["first.txt", "second.txt"])

    async def test_http_response_and_duplicate_request_keep_text_and_all_attachments(self):
        agent = chat_test.AgentChatFlowTests()._build_agent(chat_test.InMemoryMessageStorage(), chat_test.CapturingModelClient([]))
        agent._drive_claimed_turn = self.events
        adapter = ApiChannelAdapter(agent, contacts_file=self.root / "contacts.json", tasks_dir=self.root / "tasks")
        app = FastAPI()
        adapter.register_routes(app)
        request = {"user_message": "Send reports", "user_id": "alice", "event_id": "reports"}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/chat", json=request)
            duplicate = await client.post("/chat", json=request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), duplicate.json())
        self.assertEqual(response.json()["reply"], "Both reports are ready.")
        self.assertEqual([attachment["path"] for attachment in response.json()["attachments"]], ["first.txt", "second.txt"])


if __name__ == "__main__":
    unittest.main()
