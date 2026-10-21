"""Busy-message behavior for Web, WeChat, the API, and the CLI."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from xagent.core.agent import Agent
from xagent.core.config import AgentConfig
from xagent.core.handlers.model import ModelStreamEvent
from xagent.core.inbox import InboxKind
from xagent.core.runtime.client import RuntimeClient
from xagent.integrations.api.chat_service import ChatService
from xagent.integrations.api.config import ChatLimits
from xagent.integrations.weixin.adapter import WEIXIN_QUEUE_NOTICE, WEIXIN_STEER_NOTICE, WeixinAdapter
from xagent.integrations.weixin.config import WeixinAdapterConfig
from xagent.integrations.weixin.state import WeixinCredentials, WeixinStateStore
from xagent.interfaces.cli.chat import CLI_QUEUE_NOTICE, CLI_STEER_NOTICE, AgentCLI
from xagent.interfaces.server import AgentHTTPServer
from xagent.interfaces.server.models import AgentInput
from xagent.interfaces.web.proxy import _local_http_request
from tests.test_agent_chat_flow import InMemoryMessageStorage
from tests.test_agent_inbox import AgentInboxTests, _HoldTextModel
from tests.test_weixin_adapter import _FakeWeixinClient


def _build_agent(model, *, busy_input=None):
    storage = InMemoryMessageStorage()
    agent = AgentInboxTests._build_agent(None, storage, model)
    if busy_input is not None:
        agent.busy_input = busy_input
    return agent, storage


async def _events(agent, text, **kwargs):
    options = {"user_id": "alice", "channel": "cli"}
    options.update(kwargs)
    return [event async for event in agent.chat_events(user_message=text, **options)]


async def _wait_until(predicate, steps: int = 200):
    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met")


class _ThreadHoldModel(_HoldTextModel):
    """Block the first model call until another thread releases it."""

    def __init__(self):
        super().__init__()
        self.thread_started = threading.Event()
        self.thread_release = threading.Event()

    async def model_turn_events(self, messages, tool_specs, instructions=None, stream=False):
        self.calls.append(messages)
        self.instructions_calls.append(instructions)
        if len(self.calls) == 1:
            self.started.set()
            self.thread_started.set()
            while not self.thread_release.is_set():
                await asyncio.sleep(0.01)
        yield ModelStreamEvent(type="delta", delta="ok")


class OfferSteerTests(unittest.IsolatedAsyncioTestCase):
    async def test_followup_mode_refuses_public_steer_and_voice_steer_still_works(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model, busy_input=AgentConfig.BUSY_INPUT_FOLLOWUP)
        first = asyncio.create_task(_events(agent, "hello", channel="cli"))
        await model.started.wait()
        try:
            turn_id = agent.inbox.current_turn_id
            self.assertFalse(await agent.offer_steer(
                turn_id, "later", channel="cli", user_id="alice",
            ))
            self.assertFalse(agent.inbox.has_pending_steer())
            self.assertTrue(await agent.steer(
                turn_id, "voice line", channel="cli", user_id="alice",
            ))
            self.assertTrue(agent.inbox.has_pending_steer())
        finally:
            model.release.set()
            await first

    async def test_offer_steer_rejects_a_different_user(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        first = asyncio.create_task(_events(agent, "hello", channel="api"))
        await model.started.wait()
        try:
            turn_id = agent.inbox.current_turn_id
            self.assertTrue(await agent.offer_steer(
                turn_id, "join", channel="api", user_id="alice",
            ))
            self.assertFalse(await agent.offer_steer(
                turn_id, "nope", channel="api", user_id="bob",
            ))
            pending = [item.content for item in agent.inbox.take_steering()]
            self.assertEqual(pending, ["join"])
        finally:
            model.release.set()
            await first


class ApiChannelSteerTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, agent, root: Path) -> ChatService:
        return ChatService(
            agent,
            contacts_file=root / "contacts.json",
            limits=ChatLimits(),
        )

    async def _stream(self, service, text, user_id="alice"):
        data = AgentInput(user_id=user_id, user_message=text, stream=False)
        return [event async for event in service.chat_event_stream(data)]

    async def test_same_user_message_is_steered(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        with tempfile.TemporaryDirectory() as root:
            service = self._service(agent, Path(root))
            first = asyncio.create_task(self._stream(service, "hello"))
            await model.started.wait()
            try:
                second = await asyncio.wait_for(self._stream(service, "check the logs"), timeout=1)
            finally:
                model.release.set()
            first_events = await first
        self.assertEqual([event.get("type") for event in second], ["steered", "done"])
        self.assertTrue(second[-1].get("steered"))
        self.assertNotIn("check the logs", repr(model.calls[0]))
        self.assertIn("check the logs", repr(model.calls[-1]))
        self.assertFalse(any(event.get("type") == "steered" for event in first_events))

    async def test_rejected_steer_falls_back_to_the_queue(self):
        model = _HoldTextModel()
        agent, storage = _build_agent(model)

        async def reject(*_args, **_kwargs):
            return False

        agent.steer = reject
        with tempfile.TemporaryDirectory() as root:
            service = self._service(agent, Path(root))
            first = asyncio.create_task(self._stream(service, "hello"))
            await model.started.wait()
            second = asyncio.create_task(self._stream(service, "still here"))
            await _wait_until(lambda: agent.inbox.pending_count == 1 and not second.done())
            self.assertNotIn("still here", repr(model.calls[0]))
            model.release.set()
            first_events, second_events = await asyncio.gather(first, second)
        self.assertFalse(any(event.get("type") == "steered" for event in [*first_events, *second_events]))
        self.assertIn("still here", repr(model.calls[1]))
        self.assertNotIn("Mid-task", repr(model.calls[1]))
        self.assertEqual(storage.messages[1].metadata.get("inbox_kind"), InboxKind.USER_TURN.value)

    async def test_followup_mode_queues_same_user(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model, busy_input=AgentConfig.BUSY_INPUT_FOLLOWUP)
        with tempfile.TemporaryDirectory() as root:
            service = self._service(agent, Path(root))
            first = asyncio.create_task(self._stream(service, "hello"))
            await model.started.wait()
            second = asyncio.create_task(self._stream(service, "next please"))
            await _wait_until(lambda: agent.inbox.pending_count == 1 and not second.done())
            self.assertNotIn("next please", repr(model.calls[0]))
            model.release.set()
            first_events, second_events = await asyncio.gather(first, second)
        self.assertEqual(len(model.calls), 2)
        self.assertIn("next please", repr(model.calls[1]))
        self.assertNotIn("Mid-task", repr(model.calls[1]))
        self.assertFalse(any(event.get("type") == "steered" for event in [*first_events, *second_events]))

    async def test_other_user_stays_out_of_the_running_turn(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        with tempfile.TemporaryDirectory() as root:
            service = self._service(agent, Path(root))
            first = asyncio.create_task(self._stream(service, "hello"))
            await model.started.wait()
            other = asyncio.create_task(self._stream(service, "from bob", user_id="bob"))
            await _wait_until(lambda: agent.inbox.pending_count == 1 and not other.done())
            self.assertNotIn("from bob", repr(model.calls[0]))
            model.release.set()
            await asyncio.gather(first, other)
        self.assertEqual(len(model.calls), 2)
        self.assertNotIn("from bob", repr(model.calls[0]))
        self.assertIn("from bob", repr(model.calls[1]))

    async def test_sync_reply_marks_a_steered_message(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        with tempfile.TemporaryDirectory() as root:
            service = self._service(agent, Path(root))
            first = asyncio.create_task(self._stream(service, "hello"))
            await model.started.wait()
            try:
                payload = await service.run_chat_response(AgentInput(
                    user_id="alice", user_message="join in",
                ))
            finally:
                model.release.set()
                await first
        self.assertTrue(payload.get("steered"))
        self.assertEqual(payload.get("reply"), "")
        self.assertIn("join in", repr(model.calls[-1]))


class ApiSocketSteerTests(unittest.TestCase):
    def _server(self, root: str, agent: Agent) -> AgentHTTPServer:
        agent.workspace = root
        agent.config_dir = root
        agent.workspace_dir = Path(root) / "workspace"
        return AgentHTTPServer(agent=agent, config_dir=root)

    def _read_until_turn_done(self, websocket):
        events = []
        while True:
            event = websocket.receive_json()
            events.append(event)
            if event.get("type") == "done" and not event.get("steered"):
                return events

    def test_websocket_steers_a_message_sent_during_the_reply(self):
        model = _ThreadHoldModel()
        agent, _storage = _build_agent(model)
        with tempfile.TemporaryDirectory() as root:
            server = self._server(root, agent)
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws/chat") as websocket:
                    websocket.send_json({"user_id": "alice", "user_message": "hello", "stream": False})
                    accepted = websocket.receive_json()
                    self.assertEqual(accepted["type"], "accepted")
                    self.assertTrue(model.thread_started.wait(2))
                    websocket.send_json({
                        "user_id": "alice",
                        "user_message": "check the logs",
                        "stream": False,
                    })
                    steered = websocket.receive_json()
                    done = websocket.receive_json()
                    self.assertEqual(steered["type"], "steered")
                    self.assertEqual(steered["content"], "check the logs")
                    self.assertEqual(done["type"], "done")
                    self.assertTrue(done.get("steered"))
                    self.assertNotIn("check the logs", repr(model.calls[0]))
                    model.thread_release.set()
                    rest = self._read_until_turn_done(websocket)
        self.assertIn("check the logs", repr(model.calls[-1]))
        self.assertTrue(any(event.get("type") == "message_done" for event in rest))

    def test_websocket_queues_when_steer_is_rejected(self):
        model = _ThreadHoldModel()
        agent, _storage = _build_agent(model)

        async def reject(*_args, **_kwargs):
            return False

        agent.steer = reject
        with tempfile.TemporaryDirectory() as root:
            server = self._server(root, agent)
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws/chat") as websocket:
                    websocket.send_json({"user_id": "alice", "user_message": "hello", "stream": False})
                    self.assertEqual(websocket.receive_json()["type"], "accepted")
                    self.assertTrue(model.thread_started.wait(2))
                    websocket.send_json({
                        "user_id": "alice",
                        "user_message": "still here",
                        "stream": False,
                    })
                    queued = websocket.receive_json()
                    self.assertEqual(queued["type"], "queued")
                    self.assertEqual(queued["content"], "still here")
                    self.assertNotIn("still here", repr(model.calls[0]))
                    model.thread_release.set()
                    first_rest = self._read_until_turn_done(websocket)
                    second_rest = self._read_until_turn_done(websocket)
        self.assertTrue(any(event.get("type") == "message_done" for event in first_rest))
        self.assertTrue(any(
            event.get("type") == "message_done" and "ok" in str(event.get("content") or "")
            for event in second_rest
        ))
        self.assertIn("still here", repr(model.calls[1]))
        self.assertNotIn("Mid-task", repr(model.calls[1]))

    def test_websocket_followup_mode_does_not_inject(self):
        model = _ThreadHoldModel()
        agent, _storage = _build_agent(model, busy_input=AgentConfig.BUSY_INPUT_FOLLOWUP)
        with tempfile.TemporaryDirectory() as root:
            server = self._server(root, agent)
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws/chat") as websocket:
                    websocket.send_json({"user_id": "alice", "user_message": "hello", "stream": False})
                    self.assertEqual(websocket.receive_json()["type"], "accepted")
                    self.assertTrue(model.thread_started.wait(2))
                    websocket.send_json({
                        "user_id": "alice",
                        "user_message": "next please",
                        "stream": False,
                    })
                    queued = websocket.receive_json()
                    self.assertEqual(queued["type"], "queued")
                    self.assertNotIn("next please", repr(model.calls[0]))
                    model.thread_release.set()
                    self._read_until_turn_done(websocket)
                    self._read_until_turn_done(websocket)
        self.assertNotIn("next please", repr(model.calls[0]))
        self.assertIn("next please", repr(model.calls[1]))
        self.assertNotIn("Mid-task", repr(model.calls[1]))

    def test_websocket_keeps_another_user_queued(self):
        model = _ThreadHoldModel()
        agent, _storage = _build_agent(model)
        with tempfile.TemporaryDirectory() as root:
            server = self._server(root, agent)
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws/chat") as websocket:
                    websocket.send_json({"user_id": "alice", "user_message": "hello", "stream": False})
                    self.assertEqual(websocket.receive_json()["type"], "accepted")
                    self.assertTrue(model.thread_started.wait(2))
                    websocket.send_json({
                        "user_id": "bob",
                        "user_message": "from bob",
                        "stream": False,
                    })
                    queued = websocket.receive_json()
                    self.assertEqual(queued["type"], "queued")
                    self.assertEqual(queued["content"], "from bob")
                    self.assertNotEqual(queued.get("type"), "steered")
                    self.assertNotIn("from bob", repr(model.calls[0]))
                    model.thread_release.set()
                    self._read_until_turn_done(websocket)
                    self._read_until_turn_done(websocket)
        self.assertNotIn("from bob", repr(model.calls[0]))
        self.assertIn("from bob", repr(model.calls[1]))

    def test_http_steer_uses_offer_steer_and_followup_refuses(self):
        model = _ThreadHoldModel()
        agent, _storage = _build_agent(model)
        with tempfile.TemporaryDirectory() as root:
            server = self._server(root, agent)
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws/chat") as websocket:
                    websocket.send_json({"user_id": "alice", "user_message": "hello", "stream": False})
                    accepted = websocket.receive_json()
                    self.assertTrue(model.thread_started.wait(2))
                    joined = client.post("/chat/steer", json={
                        "turn_id": accepted["turn_id"],
                        "content": "join in",
                        "user_id": "alice",
                    })
                    other = client.post("/chat/steer", json={
                        "turn_id": accepted["turn_id"],
                        "content": "from bob",
                        "user_id": "bob",
                    })
                    self.assertEqual(joined.status_code, 200)
                    self.assertTrue(joined.json()["steered"])
                    self.assertFalse(other.json()["steered"])
                    model.thread_release.set()
                    self._read_until_turn_done(websocket)
        self.assertIn("join in", repr(model.calls[-1]))
        self.assertNotIn("from bob", repr(model.calls[-1]))

        follow_model = _ThreadHoldModel()
        follow_agent, _storage = _build_agent(follow_model, busy_input=AgentConfig.BUSY_INPUT_FOLLOWUP)
        with tempfile.TemporaryDirectory() as root:
            server = self._server(root, follow_agent)
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws/chat") as websocket:
                    websocket.send_json({"user_id": "alice", "user_message": "hello", "stream": False})
                    accepted = websocket.receive_json()
                    self.assertTrue(follow_model.thread_started.wait(2))
                    refused = client.post("/chat/steer", json={
                        "turn_id": accepted["turn_id"],
                        "content": "later",
                        "user_id": "alice",
                    })
                    self.assertFalse(refused.json()["steered"])
                    self.assertFalse(follow_agent.inbox.has_pending_steer())
                    follow_model.thread_release.set()
                    self._read_until_turn_done(websocket)
        self.assertNotIn("later", repr(follow_model.calls[0]))


class WebProxySteerTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_steer_route_calls_the_runtime_client(self):
        class FakeRequest:
            async def json(self):
                return {"turn_id": "turn-1", "content": "join in", "user_id": "web_user"}

        client = SimpleNamespace(steer=AsyncMock(return_value={"steered": True, "turn_id": "turn-1"}))
        with patch("xagent.core.runtime.client.RuntimeClient", return_value=client):
            response = await _local_http_request(FakeRequest(), "/chat/steer", Path("/tmp/xagent-steer"))
        self.assertEqual(response.status_code, 200)
        client.steer.assert_awaited_once_with(
            turn_id="turn-1",
            content="join in",
            channel="api",
            user_id="web_user",
            room_name=None,
        )

    async def test_runtime_client_posts_the_steer_body(self):
        client = RuntimeClient.__new__(RuntimeClient)
        response = SimpleNamespace(json=lambda: {"steered": True})
        client.request = AsyncMock(return_value=response)
        result = await client.steer(
            turn_id="turn-1",
            content="join in",
            channel="api",
            user_id="web_user",
        )
        self.assertTrue(result["steered"])
        body = client.request.await_args.kwargs["json"]
        self.assertEqual(body, {
            "turn_id": "turn-1",
            "content": "join in",
            "channel": "api",
            "user_id": "web_user",
        })
        self.assertEqual(client.request.await_args.args[:2], ("POST", "/runtime/turns/steer"))


def _weixin_message(message_id, user_id, text):
    return {
        "message_id": message_id,
        "from_user_id": user_id,
        "to_user_id": "bot@im.bot",
        "message_type": 1,
        "context_token": "ctx-owner",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


def _weixin_adapter(root, agent, *, owner_only=True):
    state = WeixinStateStore(root)
    credentials = WeixinCredentials(
        token="token",
        base_url="https://example.test",
        account_id="bot@im.bot",
        user_id="owner@im.wechat",
    )
    state.save_credentials(credentials)
    client = _FakeWeixinClient()
    config = WeixinAdapterConfig(
        account_id="bot@im.bot",
        owner_user_id="owner@im.wechat",
        owner_only=owner_only,
        send_typing=False,
        media_enabled=False,
        text_max_chars=2000,
        send_chunk_delay_seconds=0,
    )
    adapter = WeixinAdapter(
        agent=agent,
        config=config,
        runtime_dir=root,
        state_store=state,
        client=client,
    )
    adapter._credentials = credentials
    return adapter, client


def _sent(client):
    return [item["text"] for item in client.sent_text]


class WeixinChannelSteerTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_user_text_joins_the_running_turn(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        with tempfile.TemporaryDirectory() as root:
            adapter, client = _weixin_adapter(root, agent)
            first = asyncio.create_task(adapter._process_message(
                _weixin_message("1", "owner@im.wechat", "hello"),
            ))
            await model.started.wait()
            await adapter._process_message(_weixin_message("2", "owner@im.wechat", "check the logs"))
            self.assertIn(WEIXIN_STEER_NOTICE, _sent(client))
            self.assertNotIn("check the logs", repr(model.calls[0]))
            model.release.set()
            await first
        self.assertIn("check the logs", repr(model.calls[-1]))

    async def test_rejected_steer_tells_the_user_it_is_next(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)

        async def reject(*_args, **_kwargs):
            return False

        agent.steer = reject
        with tempfile.TemporaryDirectory() as root:
            adapter, client = _weixin_adapter(root, agent)
            first = asyncio.create_task(adapter._process_message(
                _weixin_message("1", "owner@im.wechat", "hello"),
            ))
            await model.started.wait()
            second = asyncio.create_task(adapter._process_message(
                _weixin_message("2", "owner@im.wechat", "still here"),
            ))
            await _wait_until(lambda: WEIXIN_QUEUE_NOTICE in _sent(client) and agent.inbox.pending_count == 1)
            self.assertNotIn(WEIXIN_STEER_NOTICE, _sent(client))
            self.assertNotIn("still here", repr(model.calls[0]))
            model.release.set()
            await asyncio.gather(first, second)
        self.assertIn("still here", repr(model.calls[1]))
        self.assertNotIn("Mid-task", repr(model.calls[1]))

    async def test_followup_mode_keeps_the_per_user_lock(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model, busy_input=AgentConfig.BUSY_INPUT_FOLLOWUP)
        with tempfile.TemporaryDirectory() as root:
            adapter, client = _weixin_adapter(root, agent)
            first = asyncio.create_task(adapter._process_message(
                _weixin_message("1", "owner@im.wechat", "hello"),
            ))
            await model.started.wait()
            second = asyncio.create_task(adapter._process_message(
                _weixin_message("2", "owner@im.wechat", "next please"),
            ))
            await asyncio.sleep(0.05)
            self.assertFalse(second.done())
            self.assertEqual(agent.inbox.pending_count, 0)
            self.assertEqual(len(model.calls), 1)
            self.assertEqual(_sent(client), [])
            model.release.set()
            await asyncio.gather(first, second)
        self.assertNotIn(WEIXIN_STEER_NOTICE, _sent(client))
        self.assertNotIn(WEIXIN_QUEUE_NOTICE, _sent(client))
        self.assertIn("next please", repr(model.calls[1]))
        self.assertNotIn("Mid-task", repr(model.calls[1]))

    async def test_other_user_is_not_injected(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        with tempfile.TemporaryDirectory() as root:
            adapter, client = _weixin_adapter(root, agent, owner_only=False)
            first = asyncio.create_task(adapter._process_message(
                _weixin_message("1", "owner@im.wechat", "hello"),
            ))
            await model.started.wait()
            other = asyncio.create_task(adapter._process_message(
                _weixin_message("2", "bob@im.wechat", "from bob"),
            ))
            await _wait_until(lambda: agent.inbox.pending_count == 1 and not other.done())
            self.assertNotIn("from bob", repr(model.calls[0]))
            self.assertNotIn(WEIXIN_STEER_NOTICE, _sent(client))
            model.release.set()
            await asyncio.gather(first, other)
        self.assertNotIn("from bob", repr(model.calls[0]))
        self.assertIn("from bob", repr(model.calls[1]))
        self.assertNotIn(WEIXIN_STEER_NOTICE, _sent(client))


class CliChannelSteerTests(unittest.IsolatedAsyncioTestCase):
    def _cli(self, agent) -> AgentCLI:
        cli = AgentCLI.__new__(AgentCLI)
        cli.agent = agent
        cli.message_storage = SimpleNamespace()
        cli.workspace_dir = None
        cli._pending_cli_turns = []
        return cli

    async def test_submit_while_busy_steers_the_same_user(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        cli = self._cli(agent)
        first = asyncio.create_task(_events(agent, "hello"))
        await model.started.wait()
        try:
            status = await cli.submit_while_busy("check the logs", "alice")
            self.assertEqual(status, "steered")
            self.assertNotIn("check the logs", repr(model.calls[0]))
        finally:
            model.release.set()
            await first
            await cli._flush_pending_cli_turns()
        self.assertIn("check the logs", repr(model.calls[-1]))

    async def test_submit_while_busy_queues_when_steer_is_rejected(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)

        async def reject(*_args, **_kwargs):
            return False

        agent.steer = reject
        cli = self._cli(agent)
        first = asyncio.create_task(_events(agent, "hello"))
        await model.started.wait()
        status = await cli.submit_while_busy("still here", "alice")
        await _wait_until(lambda: agent.inbox.pending_count == 1)
        self.assertEqual(status, "queued")
        self.assertNotIn("still here", repr(model.calls[0]))
        model.release.set()
        await first
        await cli._flush_pending_cli_turns()
        self.assertIn("still here", repr(model.calls[1]))
        self.assertNotIn("Mid-task", repr(model.calls[1]))

    async def test_submit_while_busy_follows_followup_mode(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model, busy_input=AgentConfig.BUSY_INPUT_FOLLOWUP)
        cli = self._cli(agent)
        first = asyncio.create_task(_events(agent, "hello"))
        await model.started.wait()
        status = await cli.submit_while_busy("next please", "alice")
        await _wait_until(lambda: agent.inbox.pending_count == 1)
        self.assertEqual(status, "queued")
        self.assertNotIn("next please", repr(model.calls[0]))
        model.release.set()
        await first
        await cli._flush_pending_cli_turns()
        self.assertIn("next please", repr(model.calls[1]))
        self.assertNotIn("Mid-task", repr(model.calls[1]))

    async def test_submit_while_busy_does_not_inject_another_user(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        cli = self._cli(agent)
        first = asyncio.create_task(_events(agent, "hello"))
        await model.started.wait()
        status = await cli.submit_while_busy("from bob", "bob")
        await _wait_until(lambda: agent.inbox.pending_count == 1)
        self.assertEqual(status, "queued")
        self.assertNotIn("from bob", repr(model.calls[0]))
        model.release.set()
        await first
        await cli._flush_pending_cli_turns()
        self.assertNotIn("from bob", repr(model.calls[0]))
        self.assertIn("from bob", repr(model.calls[1]))

    async def test_interactive_terminal_submits_a_line_during_the_reply(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        cli = self._cli(agent)
        ui = MagicMock()
        ui.console = None
        lines = asyncio.Queue()
        await lines.put("hello")
        task = asyncio.create_task(cli._chat_interactive_tty(
            ui,
            user_id="alice",
            stream=False,
            verbose_mode=False,
            lines=lines,
        ))
        await model.started.wait()
        await lines.put("check the logs")
        await _wait_until(lambda: any(
            call.args and call.args[0] == CLI_STEER_NOTICE
            for call in ui.print_panel.call_args_list
        ))
        model.release.set()
        await _wait_until(lambda: not agent.inbox.busy and len(model.calls) >= 2)
        await asyncio.sleep(0.05)
        await lines.put("bye")
        await asyncio.wait_for(task, timeout=2)
        self.assertIn("check the logs", repr(model.calls[-1]))
        self.assertIn(
            "Thank you for using xAgent CLI. See you next time.",
            [call.args[0] for call in ui.print_panel.call_args_list if call.args],
        )

    def test_help_documents_the_noninteractive_limit(self):
        cli = AgentCLI.__new__(AgentCLI)
        cli.agent = SimpleNamespace(tools={})
        ui = MagicMock()
        cli._show_terminal_help(ui)
        text = ui.print_panel.call_args.args[0]
        self.assertIn("non-interactive terminal cannot read a line during output", text)
        self.assertIn("joins the running task", text)

