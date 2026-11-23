"""Proactive messages use the existing channel without durable follow-up state."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from tests import test_weixin_adapter as fixtures
from xagent.core.runtime.subconscious import SubconsciousLoop


class WeixinProactiveTests(unittest.IsolatedAsyncioTestCase):
    async def run_message(self, *, responses=None, text='hello', chunk_size=2000):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        adapter, agent, client, _ = fixtures.WeixinAdapterTests()._adapter(
            root.name, text_max_chars=chunk_size)
        adapter._context_tokens['owner@im.wechat'] = 'context'
        client.send_text_message = AsyncMock(side_effect=responses)
        loop = SubconsciousLoop(agent, workspace=Path(root.name), interval_seconds=60,
            delivery_sink=adapter.deliver_subconscious_message, deliverable_channels={'weixin'})
        loop.record_interaction('weixin', 'owner@im.wechat', {})
        loop._generate_subconscious_thought = AsyncMock(return_value={
            'note': None, 'message': {'recipient': 'weixin:owner@im.wechat', 'text': text}})
        loop._delivery_retry_delay_seconds = 0
        loop._next_think_at = 0
        await loop.maybe_think()
        self.assertFalse((Path(root.name) / '.runtime' / 'followups.sqlite3').exists())
        return client

    async def test_success_uses_existing_sender_once(self):
        client = await self.run_message(responses=[{}])
        client.send_text_message.assert_awaited_once()

    async def test_lost_response_is_not_retried_by_either_layer(self):
        client = await self.run_message(responses=[TimeoutError('response lost')])
        client.send_text_message.assert_awaited_once()

    async def test_business_rejection_is_not_retried_by_either_layer(self):
        client = await self.run_message(responses=[{'ret': -2, 'errmsg': 'prepare failed'}])
        client.send_text_message.assert_awaited_once()

    async def test_partial_send_does_not_repeat_confirmed_text(self):
        client = await self.run_message(responses=[{}, TimeoutError('second part lost')],
                                        text='abcdefgh', chunk_size=4)
        self.assertEqual(client.send_text_message.await_count, 2)
        self.assertEqual(client.send_text_message.call_args_list[0].kwargs['text'], 'abcd')
