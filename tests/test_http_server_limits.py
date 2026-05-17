import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from xagent.interfaces.server import AgentHTTPServer
from xagent.schemas import AgentTurnResult


class FakeMessageStorage:
    async def clear_messages(self):
        return None


class BlockingAgent:
    model = "test-model"
    tools = {}

    def __init__(self):
        self.message_storage = FakeMessageStorage()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def __call__(self, **kwargs):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "ok"


class SlowStreamingAgent:
    model = "test-model"
    tools = {}

    def __init__(self):
        self.message_storage = FakeMessageStorage()

    async def __call__(self, **kwargs):
        return "ok"

    async def chat_events(self, **kwargs):
        yield {"type": "message_start", "message_id": "m1", "phase": "final"}
        yield {"type": "message_delta", "delta": "first", "message_id": "m1", "phase": "final"}
        await asyncio.sleep(1)
        yield {"type": "message_delta", "delta": "late", "message_id": "m1", "phase": "final"}
        yield {"type": "message_done", "message_id": "m1", "phase": "final", "content": "firstlate"}
        yield {"type": "done"}


class FastStreamingAgent:
    model = "test-model"
    tools = {}

    def __init__(self):
        self.message_storage = FakeMessageStorage()

    async def __call__(self, **kwargs):
        return "ok"

    async def chat_events(self, **kwargs):
        token_stream = bool(kwargs.get("token_stream"))
        yield {"type": "message_start", "message_id": "m1", "phase": "final"}
        if token_stream:
            yield {"type": "message_delta", "delta": "hel", "message_id": "m1", "phase": "final"}
            yield {"type": "message_delta", "delta": "lo", "message_id": "m1", "phase": "final"}
        yield {"type": "message_done", "message_id": "m1", "phase": "final", "content": "hello"}
        yield {"type": "done"}


class EventStreamingAgent(FastStreamingAgent):
    async def chat_events(self, **kwargs):
        token_stream = bool(kwargs.get("token_stream"))
        yield {"type": "message_start", "message_id": "m1", "phase": "preface"}
        if token_stream:
            yield {"type": "message_delta", "delta": "checking", "message_id": "m1", "phase": "preface"}
        yield {"type": "message_done", "message_id": "m1", "phase": "preface", "content": "checking"}
        yield {"type": "tool_call", "call_id": "call-1", "name": "run_command"}
        yield {"type": "tool_result", "call_id": "call-1", "name": "run_command"}
        yield {"type": "message_start", "message_id": "m2", "phase": "final"}
        if token_stream:
            yield {"type": "message_delta", "delta": "done", "message_id": "m2", "phase": "final"}
        yield {"type": "message_done", "message_id": "m2", "phase": "final", "content": "done"}
        yield {"type": "done"}


class FlushTrackingAgent(FastStreamingAgent):
    def __init__(self):
        super().__init__()
        self.flushed = False

    async def flush_memory(self):
        self.flushed = True


class ObservingAgent:
    model = "test-model"
    tools = {}

    def __init__(self):
        self.message_storage = FakeMessageStorage()
        self.observed_kwargs = None

    async def __call__(self, **kwargs):
        return "ok"

    async def observe(self, **kwargs):
        self.observed_kwargs = kwargs
        return AgentTurnResult(
            kind="observe",
            replied=False,
            reply=None,
            event_id=123.0,
            event_type=kwargs.get("event_type"),
            source=kwargs.get("source"),
        )


class AgentHTTPServerLimitTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, server):
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def test_chat_rejects_when_queue_timeout_expires(self):
        agent = BlockingAgent()
        server = AgentHTTPServer(
            agent=agent,
            enable_web=False,
            max_concurrent_chats=1,
            chat_queue_timeout=0.05,
            chat_timeout=1.0,
        )

        async with await self._client(server) as client:
            first = asyncio.create_task(client.post("/chat", json={
                "user_id": "alice",
                "user_message": "hold the slot",
            }))
            await asyncio.wait_for(agent.started.wait(), timeout=1.0)

            second = await client.post("/chat", json={
                "user_id": "bob",
                "user_message": "should be rejected",
            })

            agent.release.set()
            first_response = await first

        self.assertEqual(second.status_code, 429)
        self.assertIn("Too many concurrent chat requests", second.json()["detail"])
        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json(), {"reply": "ok"})

    async def test_chat_timeout_returns_gateway_timeout(self):
        agent = BlockingAgent()
        server = AgentHTTPServer(
            agent=agent,
            enable_web=False,
            max_concurrent_chats=1,
            chat_queue_timeout=1.0,
            chat_timeout=0.05,
        )

        async with await self._client(server) as client:
            response = await client.post("/chat", json={
                "user_id": "alice",
                "user_message": "timeout please",
            })

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["detail"], "Agent chat timed out.")
        self.assertTrue(agent.cancelled)

    async def test_chat_rejects_stream_field(self):
        server = AgentHTTPServer(agent=FastStreamingAgent(), enable_web=False)

        async with await self._client(server) as client:
            response = await client.post("/chat", json={
                "user_id": "alice",
                "user_message": "old stream field",
                "stream": True,
            })

        self.assertEqual(response.status_code, 422)

    async def test_chat_is_final_only_http_json(self):
        server = AgentHTTPServer(agent=EventStreamingAgent(), enable_web=False)
        async with await self._client(server) as client:
            response = await client.post("/chat", json={
                "user_id": "alice",
                "user_message": "where are we",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"reply": "ok"})

    async def test_observe_endpoint_returns_ingestion_result(self):
        agent = ObservingAgent()
        server = AgentHTTPServer(agent=agent, enable_web=False)

        async with await self._client(server) as client:
            response = await client.post("/observe", json={
                "context": "看到有人靠近门口。",
                "source": "camera",
                "event_type": "presence",
                "metadata": {"memory_policy": "always"},
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "kind": "observe",
            "replied": False,
            "reply": None,
            "event_id": 123.0,
            "event_type": "presence",
            "source": "camera",
        })
        self.assertEqual(agent.observed_kwargs["context"], "看到有人靠近门口。")
        self.assertEqual(agent.observed_kwargs["metadata"], {"memory_policy": "always"})


class AgentWebSocketServerTests(unittest.TestCase):
    def test_lifespan_starts_and_stops_runtime_heartbeat(self):
        class FakeHeartbeat:
            interval_seconds = 300

            def __init__(self):
                self.started = False
                self.stopped = False

            async def start(self):
                self.started = True

            async def stop(self):
                self.stopped = True

        agent = FlushTrackingAgent()
        heartbeat = FakeHeartbeat()
        server = AgentHTTPServer(agent=agent, enable_web=False)

        with patch("xagent.interfaces.server.create_runtime_heartbeat", return_value=heartbeat) as factory:
            with TestClient(server.app):
                pass

        factory.assert_called_once()
        self.assertTrue(heartbeat.started)
        self.assertTrue(heartbeat.stopped)
        self.assertTrue(agent.flushed)

    def test_shutdown_flushes_agent_memory(self):
        agent = FlushTrackingAgent()
        server = AgentHTTPServer(agent=agent, enable_web=False)

        with TestClient(server.app):
            pass

        self.assertTrue(agent.flushed)

    def test_websocket_chat_token_stream_false_returns_done_boundaries(self):
        server = AgentHTTPServer(agent=FastStreamingAgent(), enable_web=False)

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws/chat") as websocket:
                websocket.send_json({
                    "user_id": "alice",
                    "user_message": "hello",
                    "token_stream": False,
                })

                self.assertEqual(websocket.receive_json(), {
                    "type": "message_start",
                    "message_id": "m1",
                    "phase": "final",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "message_done",
                    "message_id": "m1",
                    "phase": "final",
                    "content": "hello",
                })
                self.assertEqual(websocket.receive_json(), {"type": "done"})

    def test_websocket_chat_token_stream_true_emits_deltas_and_done(self):
        server = AgentHTTPServer(agent=FastStreamingAgent(), enable_web=False)

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws/chat") as websocket:
                websocket.send_json({
                    "user_id": "alice",
                    "user_message": "token stream please",
                    "token_stream": True,
                })

                self.assertEqual(websocket.receive_json(), {
                    "type": "message_start",
                    "message_id": "m1",
                    "phase": "final",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "message_delta",
                    "delta": "hel",
                    "message_id": "m1",
                    "phase": "final",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "message_delta",
                    "delta": "lo",
                    "message_id": "m1",
                    "phase": "final",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "message_done",
                    "message_id": "m1",
                    "phase": "final",
                    "content": "hello",
                })
                self.assertEqual(websocket.receive_json(), {"type": "done"})

    def test_websocket_chat_emits_structured_events(self):
        server = AgentHTTPServer(agent=EventStreamingAgent(), enable_web=False)

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws/chat") as websocket:
                websocket.send_json({
                    "user_id": "alice",
                    "user_message": "where are we",
                    "token_stream": True,
                })

                self.assertEqual(websocket.receive_json(), {
                    "type": "message_start",
                    "message_id": "m1",
                    "phase": "preface",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "message_delta",
                    "delta": "checking",
                    "message_id": "m1",
                    "phase": "preface",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "message_done",
                    "message_id": "m1",
                    "phase": "preface",
                    "content": "checking",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "tool_call",
                    "call_id": "call-1",
                    "name": "run_command",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "tool_result",
                    "call_id": "call-1",
                    "name": "run_command",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "message_start",
                    "message_id": "m2",
                    "phase": "final",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "message_delta",
                    "delta": "done",
                    "message_id": "m2",
                    "phase": "final",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "message_done",
                    "message_id": "m2",
                    "phase": "final",
                    "content": "done",
                })
                self.assertEqual(websocket.receive_json(), {"type": "done"})

    def test_websocket_chat_timeout_emits_error_and_done(self):
        server = AgentHTTPServer(
            agent=SlowStreamingAgent(),
            enable_web=False,
            max_concurrent_chats=1,
            chat_queue_timeout=1.0,
            chat_timeout=0.05,
        )

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws/chat") as websocket:
                websocket.send_json({
                    "user_id": "alice",
                    "user_message": "token stream timeout",
                    "token_stream": True,
                })

                self.assertEqual(websocket.receive_json(), {
                    "type": "message_start",
                    "message_id": "m1",
                    "phase": "final",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "message_delta",
                    "delta": "first",
                    "message_id": "m1",
                    "phase": "final",
                })
                self.assertEqual(websocket.receive_json(), {
                    "type": "error",
                    "error": "Agent chat timed out.",
                    "status_code": 504,
                })
                self.assertEqual(websocket.receive_json(), {"type": "done"})

    def test_websocket_observe_returns_result_and_done(self):
        agent = ObservingAgent()
        server = AgentHTTPServer(agent=agent, enable_web=False)

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws/observe") as websocket:
                websocket.send_json({
                    "context": "灯开了。",
                    "source": "sensor",
                    "event_type": "light",
                    "metadata": {"room": "study"},
                })

                self.assertEqual(websocket.receive_json(), {
                    "type": "result",
                    "result": {
                        "kind": "observe",
                        "replied": False,
                        "reply": None,
                        "event_id": 123.0,
                        "event_type": "light",
                        "source": "sensor",
                    },
                })
                self.assertEqual(websocket.receive_json(), {"type": "done"})

        self.assertEqual(agent.observed_kwargs["context"], "灯开了。")
        self.assertEqual(agent.observed_kwargs["metadata"], {"room": "study"})


if __name__ == "__main__":
    unittest.main()
