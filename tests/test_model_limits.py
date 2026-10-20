"""Budget and retry regressions using local transports, without remote services."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from xagent.core.agent import Agent
from xagent.core.config import AgentConfig, ReplyType
from xagent.core.handlers.model import ModelClient
from xagent.core.providers import (
    MODEL_API_ANTHROPIC_MESSAGES,
    MODEL_API_OPENAI_CHAT_COMPLETIONS,
    MODEL_API_OPENAI_RESPONSES,
    PROVIDER_DEEPSEEK,
    ReasoningConfig,
)
from xagent.core.runtime.subconscious import SubconsciousLoop
from xagent.core.runtime.tasks import collect_scheduled_turn_events
from xagent.core.runtime.task_receipts import DeliveryUncertainError
from xagent.integrations.api.chat_service import ChatService, PublicHTTPException
from xagent.integrations.api.config import ChatLimits
from xagent.interfaces.server.models import ChatInput


APIS = (MODEL_API_OPENAI_RESPONSES, MODEL_API_OPENAI_CHAT_COMPLETIONS, MODEL_API_ANTHROPIC_MESSAGES)


def sdk_client(api, handler):
    client_type = AsyncAnthropic if api == MODEL_API_ANTHROPIC_MESSAGES else AsyncOpenAI
    return client_type(api_key="local-test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def success_response(api, text="ok"):
    if api == MODEL_API_ANTHROPIC_MESSAGES:
        payload = {
            "id": "msg_test", "type": "message", "role": "assistant", "model": "test",
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    elif api == MODEL_API_OPENAI_RESPONSES:
        payload = {
            "id": "resp_test", "object": "response", "created_at": 1, "model": "test", "status": "completed",
            "output": [{"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        }
    else:
        payload = {
            "id": "chat_test", "object": "chat.completion", "created": 1, "model": "test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        }
    return httpx.Response(200, json=payload)


def delta_chunk(api, text):
    if api == MODEL_API_OPENAI_RESPONSES:
        return {"type": "response.output_text.delta", "delta": text}
    if api == MODEL_API_ANTHROPIC_MESSAGES:
        return {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))])


class LocalStream:
    def __init__(self, api, *, text="first", error=None, delay=0.0, count=1):
        self.api, self.text, self.error = api, text, error
        self.delay, self.count = delay, count
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.count:
            self.count -= 1
            await asyncio.sleep(self.delay)
            return delta_chunk(self.api, self.text)
        if self.error:
            raise self.error
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


def fake_client(endpoint):
    return SimpleNamespace(responses=endpoint, messages=endpoint, chat=SimpleNamespace(completions=endpoint))


class ModelLimitsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (("RETRY_MIN_WAIT", 0), ("RETRY_MAX_WAIT", 0)):
            patcher = patch.object(AgentConfig, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_sdk_retries_do_not_multiply_framework_attempts(self):
        for api in APIS:
            for stream in (False, True):
                for status, expected in ((503, 3), (401, 1)):
                    with self.subTest(api=api, stream=stream, status=status):
                        requests = []

                        def handle(request):
                            requests.append(request)
                            return httpx.Response(status, json={"error": {"type": "api_error", "message": "simulated"}})

                        async with sdk_client(api, handle) as client:
                            model = ModelClient(client=client, model="test", model_api=api)
                            events = [event async for event in model.model_turn_events(
                                [{"role": "user", "content": "hello"}], None, stream=stream,
                            )]
                            self.assertEqual(events[-1].type, "error")
                            self.assertEqual(len(requests), expected)
                            self.assertEqual(client.max_retries, 2)

    async def test_agent_roles_use_independent_budgets_on_all_apis(self):
        thought = json.dumps({"internal_content": "A quiet thought.", "worthy": False,
                              "external_content": None, "recipient_hint": None})
        for api in APIS:
            for configured, expected in ((None, (8192, 4096, 2048)), (16000, (16000, 4096, 2048)), (1024, (1024, 1024, 1024))):
                with self.subTest(api=api, configured=configured), tempfile.TemporaryDirectory() as root:
                    requests = []

                    def handle(request):
                        requests.append(json.loads(request.content))
                        return success_response(api, thought)

                    async with sdk_client(api, handle) as client:
                        agent = Agent(client=client, workspace=root, model_api=api,
                                      model_max_tokens=configured, supports_vision=False)
                        try:
                            result, _ = await agent.model_client.call([{"role": "user", "content": "hi"}], None)
                            self.assertEqual(result, ReplyType.SIMPLE_REPLY)
                            await agent.llm_service._call_text("Diary", "Source")
                            loop = SubconsciousLoop(agent, workspace=Path(root))
                            self.assertEqual((await loop._generate_subconscious_thought())["internal_content"], "A quiet thought.")
                            key = {MODEL_API_OPENAI_RESPONSES: "max_output_tokens",
                                   MODEL_API_OPENAI_CHAT_COMPLETIONS: "max_completion_tokens",
                                   MODEL_API_ANTHROPIC_MESSAGES: "max_tokens"}[api]
                            self.assertEqual(tuple(request[key] for request in requests), expected)
                            self.assertEqual(agent.model_client.max_tokens, expected[0])
                        finally:
                            await agent.working_context_compactor.cancel_refresh()

    async def test_bounded_background_call_does_not_change_foreground_reasoning(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return success_response(MODEL_API_ANTHROPIC_MESSAGES)

        async with sdk_client(MODEL_API_ANTHROPIC_MESSAGES, handle) as client:
            reasoning = ReasoningConfig(enabled=True, budget_tokens=8192)
            model = ModelClient(client=client, model="test", model_api=MODEL_API_ANTHROPIC_MESSAGES,
                                max_tokens=16000, reasoning=reasoning)
            events = [event async for event in model.model_turn_events(
                [{"role": "user", "content": "background"}], None,
                max_tokens=2048, reasoning=ReasoningConfig(enabled=False),
            )]
            self.assertEqual(events[0].type, "text")
            await model.call([{"role": "user", "content": "foreground"}], None)
            self.assertEqual(requests[0]["max_tokens"], 2048)
            self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
            self.assertEqual(requests[1]["max_tokens"], 16000)
            self.assertEqual(requests[1]["thinking"]["budget_tokens"], 8192)
            self.assertEqual(model.reasoning, reasoning)

    async def test_deepseek_background_explicitly_disables_default_thinking(self):
        requests = []
        thought = json.dumps({"internal_content": "A thought.", "worthy": False,
                              "external_content": None, "recipient_hint": None})

        def handle(request):
            requests.append(json.loads(request.content))
            return success_response(MODEL_API_OPENAI_CHAT_COMPLETIONS, thought)

        with tempfile.TemporaryDirectory() as root:
            async with sdk_client(MODEL_API_OPENAI_CHAT_COMPLETIONS, handle) as client:
                agent = Agent(client=client, workspace=root, provider_name=PROVIDER_DEEPSEEK,
                              model_api=MODEL_API_OPENAI_CHAT_COMPLETIONS)
                try:
                    loop = SubconsciousLoop(agent, workspace=Path(root))
                    await loop._generate_subconscious_thought()
                    await agent.model_client.call([{"role": "user", "content": "hello"}], None)
                    self.assertEqual(requests[0]["max_tokens"], 2048)
                    self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
                    self.assertEqual(requests[1]["max_tokens"], 8192)
                    self.assertNotIn("thinking", requests[1])
                    self.assertIsNone(agent.model_client.reasoning)
                finally:
                    await agent.working_context_compactor.cancel_refresh()

    async def test_stream_retries_before_output_and_closes_each_attempt(self):
        for api in APIS:
            with self.subTest(api=api):
                failed = LocalStream(api, count=0, error=ConnectionError("disconnected"))
                recovered = LocalStream(api, text="recovered")
                endpoint = SimpleNamespace(create=AsyncMock(side_effect=[failed, recovered]))
                model = ModelClient(client=fake_client(endpoint), model="test", model_api=api)
                events = [event async for event in model.stream_turn([], None)]
                self.assertEqual([event.delta for event in events], ["recovered"])
                self.assertEqual(endpoint.create.await_count, 2)
                self.assertTrue(failed.closed and recovered.closed)

    async def test_stream_never_replays_after_visible_output(self):
        for api in APIS:
            with self.subTest(api=api):
                response = LocalStream(api, error=ConnectionError("disconnected"))
                endpoint = SimpleNamespace(create=AsyncMock(return_value=response))
                model = ModelClient(client=fake_client(endpoint), model="test", model_api=api)
                events = [event async for event in model.stream_turn([], None)]
                self.assertEqual([event.type for event in events], ["delta", "error"])
                self.assertEqual(events[0].delta, "first")
                self.assertEqual(endpoint.create.await_count, 1)
                self.assertTrue(response.closed)

    async def test_nonstream_timeout_cancels_attempt_and_stops_after_three(self):
        cancelled = []

        async def hanging(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        endpoint = SimpleNamespace(create=AsyncMock(side_effect=hanging))
        model = ModelClient(client=fake_client(endpoint), model="test", request_timeout=0.01)
        result, _ = await asyncio.wait_for(model.call([], None), timeout=1)
        self.assertEqual(result, ReplyType.ERROR)
        self.assertEqual(endpoint.create.await_count, 3)
        self.assertEqual(len(cancelled), 3)

    async def test_cancelling_request_never_retries(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                started = asyncio.Event()

                async def hanging(**kwargs):
                    started.set()
                    await asyncio.Event().wait()

                endpoint = SimpleNamespace(create=AsyncMock(side_effect=hanging))
                model = ModelClient(client=fake_client(endpoint), model="test")

                async def consume():
                    return [event async for event in model.model_turn_events([], None, stream=stream)]

                task = asyncio.create_task(consume())
                await asyncio.wait_for(started.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(endpoint.create.await_count, 1)

    async def test_closing_stream_after_first_event_releases_response(self):
        response = LocalStream(MODEL_API_OPENAI_RESPONSES, count=10)
        endpoint = SimpleNamespace(create=AsyncMock(return_value=response))
        model = ModelClient(client=fake_client(endpoint), model="test", model_api=MODEL_API_OPENAI_RESPONSES)
        events = model.stream_turn([], None)
        self.assertEqual((await events.__anext__()).type, "delta")
        await events.aclose()
        self.assertTrue(response.closed)
        self.assertEqual(endpoint.create.await_count, 1)

    async def test_stream_deadline_does_not_reset_for_each_chunk(self):
        response = LocalStream(MODEL_API_OPENAI_RESPONSES, delay=0.01, count=50)
        endpoint = SimpleNamespace(create=AsyncMock(return_value=response))
        model = ModelClient(client=fake_client(endpoint), model="test", model_api=MODEL_API_OPENAI_RESPONSES,
                            request_timeout=0.06)
        started = time.monotonic()
        events = [event async for event in model.stream_turn([], None)]
        self.assertEqual(events[-1].type, "error")
        self.assertTrue(any(event.type == "delta" for event in events))
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(endpoint.create.await_count, 1)
        self.assertTrue(response.closed)

    async def test_http_allows_core_to_persist_timeout_receipt(self):
        async def hanging(**kwargs):
            await asyncio.Event().wait()

        endpoint = SimpleNamespace(create=AsyncMock(side_effect=hanging))
        with tempfile.TemporaryDirectory() as root:
            agent = Agent(client=fake_client(endpoint), workspace=root,
                          model_api=MODEL_API_OPENAI_CHAT_COMPLETIONS)
            agent.inbox.run_timeout = 0.03
            agent.inbox.queue_timeout = 0.02
            service = ChatService(agent, contacts_file=Path(root) / "contacts.json",
                                  limits=ChatLimits(chat_timeout=0.001))
            try:
                with patch.object(AgentConfig, "TRANSPORT_TIMEOUT_GRACE_SECONDS", 0.1):
                    with self.assertRaises(PublicHTTPException) as error:
                        await service.run_chat_response(ChatInput(user_id="alice", user_message="hello"))
                self.assertEqual(error.exception.payload["error_code"], "timeout")
                self.assertTrue(error.exception.payload["turn_id"])
                records = await agent.turn_store.list_records()
                self.assertEqual(records[0]["state"], "timeout")
                self.assertFalse(agent.inbox.busy)
                self.assertEqual(endpoint.create.await_count, 1)
            finally:
                await agent.working_context_compactor.cancel_refresh()

    async def test_uncertain_scheduled_result_closes_its_turn_immediately(self):
        closed = []

        async def uncertain_turn():
            try:
                yield {"type": "error", "error": "tool result is uncertain", "needs_review": True}
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        with self.assertRaises(DeliveryUncertainError):
            await collect_scheduled_turn_events(uncertain_turn())
        self.assertEqual(closed, [True])
