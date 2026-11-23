"""Empty chat responses recover inside one model call, without replaying tools."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from tests import test_model_limits as fixtures
from xagent.core.agent import Agent
from xagent.core.config import AgentConfig, ReplyType
from xagent.core.handlers.model import ModelClient
from xagent.core.providers import MODEL_API_OPENAI_CHAT_COMPLETIONS, PROVIDER_DEEPSEEK


API = MODEL_API_OPENAI_CHAT_COMPLETIONS


def response(*, reason='stop', text=None, reasoning='private reasoning', refusal=None, tools=None):
    return httpx.Response(200, json={
        'id': 'response-test', 'object': 'chat.completion', 'created': 1, 'model': 'test',
        'choices': [{'index': 0, 'finish_reason': reason, 'message': {
            'role': 'assistant', 'content': text, 'reasoning_content': reasoning,
            'refusal': refusal, 'tool_calls': tools,
        }}],
        'usage': {'prompt_tokens': 120, 'completion_tokens': 8192,
                  'total_tokens': 8312, 'completion_tokens_details': {'reasoning_tokens': 8192}},
    })


class EmptyModelResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ('RETRY_MIN_WAIT', 'RETRY_MAX_WAIT'):
            patcher = patch.object(AgentConfig, name, 0)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_sequence(self, replies, *, legacy=False):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return replies[min(len(requests) - 1, len(replies) - 1)]

        async with fixtures.sdk_client(API, handle) as client:
            model = ModelClient(client=client, model='test', provider_name=PROVIDER_DEEPSEEK, max_tokens=8192)
            with self.assertLogs('xagent.core.handlers.model', level='WARNING') as logs:
                if legacy:
                    result = await model.call([{'role': 'user', 'content': 'hello'}], None)
                else:
                    result = [event async for event in model.model_turn_events(
                        [{'role': 'user', 'content': 'hello'}], None)]
            return result, requests, '\n'.join(logs.output)

    async def test_empty_stop_then_success_reuses_request(self):
        events, requests, _ = await self.run_sequence([response(), response(text='answer')])
        self.assertEqual([e.type for e in events], ['text'])
        self.assertEqual(events[0].delta, 'answer')
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])

    async def test_repeated_empty_stops_after_one_extra_request_with_safe_diagnostics(self):
        events, requests, logs = await self.run_sequence([response(text='   ')])
        self.assertEqual(len(requests), 2)
        error = events[-1].error
        self.assertEqual(error.code, 'empty_model_response')
        details = json.loads(error.details)
        self.assertEqual(details['attempt'], 2)
        self.assertEqual(details['response_id'], 'response-test')
        self.assertEqual(details['finish_reason'], 'stop')
        self.assertEqual(details['reasoning_tokens'], 8192)
        self.assertEqual(details['max_tokens'], 8192)
        self.assertEqual(details['text_chars'], 3)
        self.assertGreater(details['reasoning_chars'], 0)
        self.assertNotIn('private reasoning', logs)
        self.assertNotIn('private reasoning', error.details)

    async def test_output_limit_filter_and_refusal_do_not_retry(self):
        for reason, refusal in [('length', None), ('content_filter', None), ('aborted', None),
                                ('stop', 'private refusal reason')]:
            with self.subTest(reason=reason, refusal=bool(refusal)):
                events, requests, logs = await self.run_sequence([response(reason=reason, refusal=refusal)])
                self.assertEqual(len(requests), 1)
                self.assertEqual(events[-1].error.code, 'empty_model_response')
                self.assertEqual(json.loads(events[-1].error.details)['finish_reason'], reason)
                self.assertNotIn('private refusal reason', logs)

    async def test_resource_failure_uses_existing_three_attempt_limit(self):
        events, requests, _ = await self.run_sequence([response(reason='insufficient_system_resource')])
        self.assertEqual(len(requests), 3)
        self.assertEqual(json.loads(events[-1].error.details)['attempt'], 3)

    async def test_resource_failure_can_recover(self):
        events, requests, _ = await self.run_sequence([
            response(reason='insufficient_system_resource'), response(text='recovered'),
        ])
        self.assertEqual([e.type for e in events], ['text'])
        self.assertEqual(events[0].delta, 'recovered')
        self.assertEqual(len(requests), 2)

    async def test_empty_and_http_errors_share_one_attempt_budget(self):
        unavailable = httpx.Response(503, json={'error': {'message': 'unavailable'}})
        events, requests, _ = await self.run_sequence([response(), unavailable, response()])
        self.assertEqual(len(requests), 3)
        self.assertEqual(events[-1].error.code, 'empty_model_response')

    async def test_legacy_call_keeps_empty_response_error_code_and_details(self):
        (kind, error), requests, _ = await self.run_sequence([response()], legacy=True)
        self.assertEqual(kind, ReplyType.ERROR)
        self.assertEqual(error.code, 'empty_model_response')
        self.assertEqual(json.loads(error.details)['attempt'], 2)
        self.assertEqual(len(requests), 2)

    async def test_tool_executes_once_when_following_model_request_is_retried(self):
        requests = []
        tool = {'id': 'write-once', 'type': 'function',
                'function': {'name': 'write_marker', 'arguments': '{}'}}
        replies = [response(reason='tool_calls', tools=[tool]), response(), response(text='Saved.')]

        def handle(request):
            requests.append(json.loads(request.content))
            return replies[len(requests) - 1]

        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'marker.txt'
            calls = []

            async def write_marker():
                calls.append(True)
                marker.write_text(str(len(calls)))
                return {'saved': True}

            write_marker.tool_spec = {'type': 'function', 'function': {
                'name': 'write_marker', 'description': 'Write a test marker.',
                'parameters': {'type': 'object', 'properties': {}},
            }}
            async with fixtures.sdk_client(API, handle) as client:
                agent = Agent(client=client, model='test', model_api=API, provider_name=PROVIDER_DEEPSEEK,
                              tools=[write_marker], workspace=root, supports_vision=False)
                try:
                    events = [event async for event in agent.chat_events(
                        user_message='Write a marker, then confirm.', user_id='alice', stream=False)]
                    self.assertFalse(any(e['type'] == 'error' for e in events))
                    self.assertEqual([e['content'] for e in events if e['type'] == 'message_done'], ['Saved.'])
                    self.assertEqual(marker.read_text(), '1')
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(len(requests), 3)
                    self.assertEqual(requests[1], requests[2])
                    self.assertTrue(any(m['role'] == 'tool' for m in requests[2]['messages']))
                finally:
                    await agent.working_context_compactor.cancel_refresh()
