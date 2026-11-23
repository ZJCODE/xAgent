"""Real iLink response handling through the durable task delivery entry point."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from tests import test_weixin_adapter as fixtures
from xagent.core.runtime.task_receipts import note_task_tool
from xagent.core.runtime.tasks import AsyncTaskScheduler, enqueue_scheduled_task
from xagent.integrations.weixin.client import WeixinClient


class WeixinDeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 11, 23, 1, 5, tzinfo=timezone.utc)
        self.responses = []
        self.requests = []

        def respond(request):
            self.requests.append(json.loads(request.content))
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return httpx.Response(200, json=response)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(self.http.aclose)
        self.adapter, _, _, _ = fixtures.WeixinAdapterTests()._adapter(self.temp.name, text_max_chars=4)
        self.adapter.client = WeixinClient(base_url='https://example.test', http_client=self.http)
        self.adapter._context_tokens['owner@im.wechat'] = 'context-before'
        self.preparations = 0

        async def execute(task):
            self.preparations += 1
            note_task_tool('run_command', 'external')
            return {'content': 'abcdefgh', 'attachments': []}

        self.scheduler = AsyncTaskScheduler(
            self.root / 'tasks', can_handle=lambda task: True,
            execute=execute, deliver=self.adapter.deliver_scheduled_task,
            now_provider=lambda: self.now,
        )
        self.task = enqueue_scheduled_task(
            tasks_dir=self.root / 'tasks', task_type='agent', content='summarize and stop probe',
            run_at=self.now, channel='weixin', target={'user_id': 'owner@im.wechat'}, timezone_name='UTC',
        )

    def run_record(self):
        row = self.scheduler.store.list_runs(self.task.task_id)[0]
        return self.scheduler.store.get_run(self.task.task_id, row['id'])

    async def test_rejection_preserves_result_and_can_retry_only_the_rejected_part(self):
        self.responses = [{'ret': 0}, {'ret': -2, 'errmsg': 'prepare failed'}]
        await self.scheduler.tick()
        run = self.run_record()
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['failure_stage'], 'deliver')
        self.assertEqual(run['result']['content'], 'abcdefgh')
        self.assertIn('ret=-2', run['error'])
        self.assertIn('session window/quota', run['error'])
        rejected = next(p for p in run['deliveries'] if p['status'] == 'failed')
        self.assertFalse(rejected['uncertain_on_error'])
        self.assertEqual(len(self.requests), 2)
        self.now += timedelta(minutes=5)
        await self.scheduler.tick()
        self.assertEqual(len(self.requests), 2)
        with self.assertRaises(ValueError):
            self.scheduler.store.act_on_run(self.task.task_id, run['id'], 'prepare', self.now)

        self.adapter._context_tokens['owner@im.wechat'] = 'context-after'
        self.responses = [{'ret': 0}]
        self.scheduler.store.act_on_run(self.task.task_id, run['id'], 'deliver', self.now)
        await self.scheduler.tick()
        self.assertEqual(self.run_record()['status'], 'succeeded')
        self.assertEqual(self.preparations, 1)
        self.assertEqual([r['msg']['item_list'][0]['text_item']['text'] for r in self.requests],
                         ['abcd', 'efgh', 'efgh'])
        self.assertEqual(self.requests[1]['msg']['client_id'], self.requests[2]['msg']['client_id'])
        self.assertEqual(self.requests[2]['msg']['context_token'], 'context-after')

    async def test_connect_failure_retries_prepared_result(self):
        self.responses = [httpx.ConnectTimeout('not connected')]
        await self.scheduler.tick()
        self.assertEqual(self.run_record()['status'], 'retry_wait')
        self.now += timedelta(seconds=30)
        self.responses = [{'ret': 0}, {'ret': 0}]
        await self.scheduler.tick()
        self.assertEqual(self.run_record()['status'], 'succeeded')
        self.assertEqual(self.preparations, 1)

    async def test_lost_response_after_a_rejected_send_still_blocks_replay(self):
        self.responses = [{'ret': -2, 'errmsg': 'prepare failed'}]
        await self.scheduler.tick()
        run = self.run_record()
        self.scheduler.store.act_on_run(self.task.task_id, run['id'], 'deliver', self.now)
        self.responses = [httpx.ReadTimeout('retry response lost')]
        await self.scheduler.tick()
        run = self.run_record()
        self.assertEqual(run['status'], 'needs_review')
        self.assertTrue(run['deliveries'][0]['uncertain_on_error'])
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.scheduler.store.act_on_run(self.task.task_id, run['id'], 'deliver', self.now)
        self.assertEqual(self.preparations, 1)

    async def test_read_timeout_remains_unknown_and_cannot_be_replayed(self):
        self.responses = [{'ret': 0}, httpx.ReadTimeout('response lost')]
        await self.scheduler.tick()
        run = self.run_record()
        self.assertEqual(run['status'], 'needs_review')
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.scheduler.store.act_on_run(self.task.task_id, run['id'], 'deliver', self.now)
        self.assertEqual(len(self.requests), 2)

    async def test_non_json_response_remains_unknown(self):
        # Simulate the client parsing an invalid send acknowledgement.
        self.adapter.client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text='<html>proxy error</html>')))
        self.addAsyncCleanup(self.adapter.client._client.aclose)
        await self.scheduler.tick()
        self.assertEqual(self.run_record()['status'], 'needs_review')

    async def test_http_server_error_remains_unknown_even_with_business_code(self):
        self.adapter.client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={'errcode': -2, 'errmsg': 'upstream error'})))
        self.addAsyncCleanup(self.adapter.client._client.aclose)
        await self.scheduler.tick()
        self.assertEqual(self.run_record()['status'], 'needs_review')

    async def test_rejection_diagnostics_do_not_store_tokens(self):
        self.responses = [{'ret': -2, 'errcode': -2,
                           'errmsg': 'prepare failed context_token=private bot_token=credential access-token=old-secret',
                           'context_token': 'never-log-this-payload'}]
        await self.scheduler.tick()
        run = self.run_record()
        self.assertIn('ret=-2 errcode=-2', run['error'])
        self.assertNotIn('private', run['error'])
        self.assertNotIn('credential', run['error'])
        self.assertNotIn('old-secret', run['error'])
        self.assertNotIn('never-log-this-payload', json.dumps(run))
