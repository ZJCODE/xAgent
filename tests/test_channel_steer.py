"""Busy-message behavior for Web's API socket, WeChat, and the CLI."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from xagent.core.agent import Agent
from xagent.core.handlers.model import ModelStreamEvent
from xagent.core.inbox import InboxKind
from xagent.core.runtime.client import RuntimeClient
from xagent.integrations.api.chat_service import ChatService
from xagent.integrations.api.config import ChatLimits
from xagent.integrations.weixin.adapter import WeixinAdapter
from xagent.integrations.weixin.config import WeixinAdapterConfig
from xagent.integrations.weixin.state import WeixinCredentials, WeixinStateStore
from xagent.interfaces.cli.chat import AgentCLI
from xagent.interfaces.server import AgentHTTPServer
from xagent.interfaces.server.models import AgentInput
from tests.test_agent_chat_flow import InMemoryMessageStorage
from tests.test_agent_inbox import AgentInboxTests, _HoldTextModel
from tests.test_weixin_adapter import _FakeWeixinClient


def _build_agent(model):
    storage = InMemoryMessageStorage()
    agent = AgentInboxTests._build_agent(None, storage, model)
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


def _reject_steer(agent):
    async def reject(*_args, **_kwargs):
        return False

    agent.steer = reject


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
        _reject_steer(agent)
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
        self.assertEqual(storage.messages[1].metadata.get("inbox_kind"), InboxKind.USER_TURN.value)

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

    def test_websocket_rejected_steer_runs_after_the_current_reply(self):
        model = _ThreadHoldModel()
        agent, _storage = _build_agent(model)
        _reject_steer(agent)
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
                    model.thread_release.set()
                    first_rest = self._read_until_turn_done(websocket)
                    second_rest = self._read_until_turn_done(websocket)
        self.assertTrue(any(event.get("type") == "message_done" for event in first_rest))
        self.assertEqual(second_rest[0]["type"], "accepted")
        self.assertFalse(any(event.get("type") == "steered" for event in [*first_rest, *second_rest]))
        self.assertIn("still here", repr(model.calls[1]))
        self.assertNotIn("still here", repr(model.calls[0]))

    def test_websocket_keeps_another_user_out_of_the_running_turn(self):
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
                    model.thread_release.set()
                    self._read_until_turn_done(websocket)
                    second_rest = self._read_until_turn_done(websocket)
        self.assertEqual(second_rest[0]["type"], "accepted")
        self.assertNotIn("from bob", repr(model.calls[0]))
        self.assertIn("from bob", repr(model.calls[1]))

    def test_runtime_client_posts_the_private_steer_route(self):
        client = RuntimeClient.__new__(RuntimeClient)
        response = SimpleNamespace(json=lambda: {"steered": True})
        client.request = AsyncMock(return_value=response)

        async def call():
            result = await client.steer(
                turn_id="turn-1",
                content="join in",
                channel="api",
                user_id="web_user",
            )
            self.assertTrue(result["steered"])

        asyncio.run(call())
        self.assertEqual(client.request.await_args.args[:2], ("POST", "/runtime/turns/steer"))
        self.assertEqual(client.request.await_args.kwargs["json"], {
            "turn_id": "turn-1",
            "content": "join in",
            "channel": "api",
            "user_id": "web_user",
        })


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


_WEIXIN_STEER_NOTICES = ("Added to the current reply.", "I'll handle that next.")
_CLI_STEER_NOTICES = ("Added to the running task.", "Will be handled next.")


def _assert_notices_absent(test, texts):
    for notice in _WEIXIN_STEER_NOTICES:
        test.assertNotIn(notice, texts)


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
            _assert_notices_absent(self, _sent(client))
            self.assertNotIn("check the logs", repr(model.calls[0]))
            model.release.set()
            await first
        self.assertIn("check the logs", repr(model.calls[-1]))

    async def test_rejected_steer_waits_in_the_queue(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        _reject_steer(agent)
        with tempfile.TemporaryDirectory() as root:
            adapter, client = _weixin_adapter(root, agent)
            first = asyncio.create_task(adapter._process_message(
                _weixin_message("1", "owner@im.wechat", "hello"),
            ))
            await model.started.wait()
            second = asyncio.create_task(adapter._process_message(
                _weixin_message("2", "owner@im.wechat", "still here"),
            ))
            await _wait_until(lambda: agent.inbox.pending_count == 1)
            _assert_notices_absent(self, _sent(client))
            self.assertNotIn("still here", repr(model.calls[0]))
            model.release.set()
            await asyncio.gather(first, second)
        self.assertIn("still here", repr(model.calls[1]))

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
            _assert_notices_absent(self, _sent(client))
            model.release.set()
            await asyncio.gather(first, other)
        self.assertNotIn("from bob", repr(model.calls[0]))
        self.assertIn("from bob", repr(model.calls[1]))
        _assert_notices_absent(self, _sent(client))


class CliChannelSteerTests(unittest.IsolatedAsyncioTestCase):
    def _cli(self, agent) -> AgentCLI:
        cli = AgentCLI.__new__(AgentCLI)
        cli.agent = agent
        cli.message_storage = SimpleNamespace()
        cli.workspace_dir = None
        cli._live_turn_id = None
        return cli

    async def test_interactive_terminal_joins_the_running_task(self):
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
        await _wait_until(lambda: agent.inbox.has_pending_steer())
        self.assertFalse(any(
            call.args and call.args[0] in _CLI_STEER_NOTICES
            for call in ui.print_panel.call_args_list
        ))
        model.release.set()
        await _wait_until(lambda: not agent.inbox.busy and len(model.calls) >= 2)
        await asyncio.sleep(0.05)
        await lines.put("bye")
        await asyncio.wait_for(task, timeout=2)
        self.assertIn("check the logs", repr(model.calls[-1]))
        self.assertNotIn("check the logs", repr(model.calls[0]))

    async def test_a_line_that_cannot_join_is_sent_after_the_reply(self):
        model = _HoldTextModel()
        agent, _storage = _build_agent(model)
        _reject_steer(agent)
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
        await lines.put("still here")
        await _wait_until(lambda: lines.empty())
        self.assertFalse(any(
            call.args and call.args[0] in _CLI_STEER_NOTICES
            for call in ui.print_panel.call_args_list
        ))
        self.assertNotIn("still here", repr(model.calls[0]))
        model.release.set()
        await _wait_until(lambda: len(model.calls) >= 2)
        await lines.put("bye")
        await asyncio.wait_for(task, timeout=2)
        self.assertNotIn("still here", repr(model.calls[0]))
        self.assertIn("still here", repr(model.calls[1]))

    def test_help_documents_the_noninteractive_limit(self):
        cli = AgentCLI.__new__(AgentCLI)
        cli.agent = SimpleNamespace(tools={})
        ui = MagicMock()
        cli._show_terminal_help(ui)
        text = ui.print_panel.call_args.args[0]
        self.assertIn("non-interactive terminal cannot read a line during output", text)
        self.assertIn("joins the running task", text)
