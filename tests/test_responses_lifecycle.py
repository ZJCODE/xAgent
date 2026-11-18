"""Responses lifecycle regressions through the real SDK and a local HTTP transport."""

import json
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from xagent.core.config import AgentConfig, ReplyType
from xagent.core.handlers.model import ModelClient
from xagent.core.providers import MODEL_API_OPENAI_RESPONSES


def response(status="completed", *, text=None, refusal=None, error=None, reason=None, tools=False):
    output = []
    if text or refusal:
        content = ({"type": "refusal", "refusal": refusal} if refusal else
                   {"type": "output_text", "text": text, "annotations": []})
        output.append({"type": "message", "id": "msg_test", "role": "assistant",
                       "status": "completed", "content": [content]})
    if tools:
        output.append({"type": "function_call", "id": "fc_test", "call_id": "call_test",
                       "name": "write_file", "arguments": '{"text":"complete"}'})
    return {
        "id": "resp_lifecycle", "object": "response", "created_at": 1, "model": "test",
        "status": status, "output": output,
        "error": {"code": error, "message": "private provider error"} if error else None,
        "incomplete_details": {"reason": reason} if reason else None,
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
                  "output_tokens_details": {"reasoning_tokens": 15}},
    }


def terminal(payload):
    return {"type": "response." + payload["status"], "response": payload, "sequence_number": 1}


def delta(text, *, refusal=False):
    return {"type": "response.refusal.delta" if refusal else "response.output_text.delta",
            "delta": text, "item_id": "msg_test", "output_index": 0, "content_index": 0,
            "sequence_number": 0}


class ResponsesLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ("RETRY_MIN_WAIT", "RETRY_MAX_WAIT"):
            patcher = patch.object(AgentConfig, name, 0)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_model(self, replies, *, mode="stream"):
        requests = []

        def handle(request):
            index = len(requests)
            requests.append(request)
            payload = replies[min(index, len(replies) - 1)]
            if mode == "stream":
                body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in payload)
                return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=payload, headers={"x-request-id": "req_lifecycle"})

        async with AsyncOpenAI(api_key="local-test-key", http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
        )) as client:
            model = ModelClient(client=client, model="test", model_api=MODEL_API_OPENAI_RESPONSES)
            if mode == "call":
                result = await model.call([], None)
            else:
                result = [event async for event in model.model_turn_events([], None, stream=mode == "stream")]
        return result, len(requests)

    async def test_refusal_is_a_reply_in_all_paths(self):
        payload = response(refusal="I cannot help with that.")
        for mode in ("stream", "events", "call"):
            for streamed_deltas in (False, True) if mode == "stream" else (False,):
                with self.subTest(mode=mode, deltas=streamed_deltas):
                    events = ([delta("I cannot ", refusal=True), delta("help with that.", refusal=True)]
                              if streamed_deltas else [])
                    replies = [events + [terminal(payload)]] if mode == "stream" else [payload]
                    result, attempts = await self.run_model(replies, mode=mode)
                    if mode == "call":
                        self.assertEqual(result, (ReplyType.SIMPLE_REPLY, "I cannot help with that."))
                    else:
                        self.assertNotIn("error", [event.type for event in result])
                        self.assertEqual("".join(event.delta for event in result), "I cannot help with that.")
                    self.assertEqual(attempts, 1)

    async def test_completed_empty_assistant_message_can_finish_silently(self):
        for content in ([], [{"type": "output_text", "text": "", "annotations": []}]):
            for mode in ("stream", "events", "call"):
                with self.subTest(content=content, mode=mode):
                    payload = response()
                    payload["output"] = [{"type": "message", "id": "msg_test", "role": "assistant",
                                          "status": "completed", "content": content}]
                    result, attempts = await self.run_model(
                        [[terminal(payload)]] if mode == "stream" else [payload], mode=mode,
                    )
                    if mode == "call":
                        self.assertEqual(result, (ReplyType.SIMPLE_REPLY, ""))
                    else:
                        self.assertEqual([event.type for event in result], ["completed"])
                    self.assertEqual(attempts, 1)

    async def test_reasoning_without_final_message_still_reports_empty_response(self):
        payload = response()
        payload["output"] = [{"type": "reasoning", "id": "rs_test", "summary": []}]
        for mode in ("stream", "events", "call"):
            with self.subTest(mode=mode):
                result, attempts = await self.run_model(
                    [[terminal(payload)]] if mode == "stream" else [payload], mode=mode,
                )
                error = result[1] if mode == "call" else result[-1].error
                self.assertEqual(error.code, "empty_stream_response" if mode == "stream" else "empty_model_response")
                self.assertEqual(attempts, 1)

    async def test_completed_response_preserves_text_and_authoritative_tool_output(self):
        payload = response(text="Checking.", tools=True)
        payload["output"].insert(0, {"type": "reasoning", "id": "rs_test",
                                     "summary": [], "encrypted_content": "replay-token"})
        prefix = [delta("Checking."), {"type": "response.output_item.added", "output_index": 2,
                  "item": {"type": "function_call", "id": "fc_test", "call_id": "call_test",
                           "name": "write_file", "arguments": '{"text":'}}]
        for deltas in ([], prefix):
            with self.subTest(deltas=bool(deltas)):
                result, attempts = await self.run_model([deltas + [terminal(payload)]])
                self.assertEqual([event.type for event in result], ["delta", "tool_calls"])
                self.assertEqual(result[0].delta, "Checking.")
                call = result[1].tool_calls[0]
                self.assertEqual(call.arguments, '{"text":"complete"}')
                self.assertEqual(call.assistant_content, "Checking.")
                self.assertEqual(call.response_items[0]["encrypted_content"], "replay-token")
                self.assertEqual(attempts, 1)

    async def test_terminal_failures_never_deliver_tools_or_retry_permanent_errors(self):
        failures = [
            (response("incomplete", reason="max_output_tokens", tools=True), "model_response_incomplete"),
            (response("incomplete", reason="content_filter", tools=True), "model_response_incomplete"),
            (response("failed", error="invalid_prompt", tools=True), "model_response_failed"),
        ]
        for mode in ("stream", "events", "call"):
            for payload, code in failures:
                with self.subTest(mode=mode, code=code, reason=payload["incomplete_details"]):
                    replies = [[terminal(payload)]] if mode == "stream" else [payload]
                    result, attempts = await self.run_model(replies, mode=mode)
                    if mode == "call":
                        self.assertEqual(result[0], ReplyType.ERROR)
                        error = result[1]
                    else:
                        self.assertEqual([event.type for event in result], ["error"])
                        error = result[0].error
                    self.assertEqual(error.code, code)
                    details = json.loads(error.details)
                    self.assertEqual(details["response_id"], "resp_lifecycle")
                    self.assertEqual(details["status"], payload["status"])
                    self.assertEqual(attempts, 1)

    async def test_transient_failures_retry_before_output_in_all_paths(self):
        for mode in ("stream", "events", "call"):
            for code in ("server_error", "rate_limit_exceeded"):
                with self.subTest(mode=mode, code=code):
                    failed = response("failed", error=code)
                    recovered = response(text="Recovered.")
                    replies = [[terminal(failed)], [terminal(recovered)]] if mode == "stream" else [failed, recovered]
                    result, attempts = await self.run_model(replies, mode=mode)
                    if mode == "call":
                        self.assertEqual(result, (ReplyType.SIMPLE_REPLY, "Recovered."))
                    else:
                        self.assertEqual("".join(event.delta for event in result), "Recovered.")
                        self.assertNotIn("error", [event.type for event in result])
                    self.assertEqual(attempts, 2)

    async def test_transient_failure_after_text_does_not_replay(self):
        result, attempts = await self.run_model([[
            delta("Already visible."), terminal(response("failed", error="server_error", tools=True)),
        ]])
        self.assertEqual([event.type for event in result], ["delta", "error"])
        self.assertEqual(result[-1].error.code, "model_response_failed")
        self.assertEqual(attempts, 1)

    async def test_incomplete_terminal_only_text_is_preserved_with_failure(self):
        result, attempts = await self.run_model([[
            terminal(response("incomplete", text="Partial answer.", reason="max_output_tokens", tools=True)),
        ]])
        self.assertEqual([event.type for event in result], ["delta", "error"])
        self.assertEqual(result[0].delta, "Partial answer.")
        self.assertEqual(result[-1].error.code, "model_response_incomplete")
        self.assertEqual(attempts, 1)

    async def test_eof_without_terminal_never_delivers_tools(self):
        created = {"type": "response.created", "response": response("in_progress")}
        tool = {"type": "response.output_item.added", "output_index": 0,
                "item": response(tools=True)["output"][0]}
        result, attempts = await self.run_model([[created, tool]])
        self.assertEqual([event.type for event in result], ["error"])
        self.assertEqual(result[0].error.code, "model_stream_failed")
        self.assertEqual(json.loads(result[0].error.details)["response_id"], "resp_lifecycle")
        self.assertEqual(attempts, AgentConfig.RETRY_ATTEMPTS)

    async def test_eof_after_text_reports_failure_without_retry(self):
        result, attempts = await self.run_model([[delta("Partial.")]])
        self.assertEqual([event.type for event in result], ["delta", "error"])
        self.assertEqual(result[-1].error.code, "model_stream_failed")
        self.assertEqual(attempts, 1)

    async def test_stream_error_event_classification_and_retry(self):
        for code, attempts in (("server_error", AgentConfig.RETRY_ATTEMPTS), ("invalid_prompt", 1)):
            with self.subTest(code=code):
                result, count = await self.run_model([[
                    {"type": "error", "code": code, "message": "private provider error", "sequence_number": 0},
                ]])
                self.assertEqual(result[-1].error.code, "model_stream_error")
                self.assertEqual(json.loads(result[-1].error.details)["error_code"], code)
                self.assertEqual(count, attempts)

    async def test_diagnostics_include_metadata_without_content(self):
        for mode in ("stream", "events", "call"):
            with self.subTest(mode=mode):
                payload = response("incomplete", text="private answer", reason="max_output_tokens")
                payload["instructions"] = "private prompt"
                payload["error"] = {"code": "invalid_prompt", "message": "private provider error"}
                with self.assertLogs("xagent.core.handlers.model", level="INFO") as logs:
                    result, _ = await self.run_model([[terminal(payload)]] if mode == "stream" else [payload], mode=mode)
                rendered = "\n".join(logs.output)
                for secret in ("private answer", "private prompt", "private provider error"):
                    self.assertNotIn(secret, rendered)
                self.assertIn("resp_lifecycle", rendered)
                self.assertIn("max_output_tokens", rendered)
                error = result[1] if mode == "call" else result[-1].error
                details = json.loads(error.details)
                self.assertEqual(details["reasoning_tokens"], 15)
                if mode != "stream":
                    self.assertEqual(details["request_id"], "req_lifecycle")

    async def test_actual_empty_completion_is_not_blindly_retried(self):
        result, attempts = await self.run_model([[terminal(response())]])
        self.assertEqual(result[0].error.code, "empty_stream_response")
        self.assertEqual(json.loads(result[0].error.details)["status"], "completed")
        self.assertEqual(attempts, 1)


if __name__ == "__main__":
    unittest.main()
