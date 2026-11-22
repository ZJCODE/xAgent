"""Cross-cycle follow-up and real adapter acknowledgement regressions."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from xagent.core.inbox import AgentInbox
from xagent.core.runtime.subconscious import SubconsciousLoop
from tests import test_weixin_adapter as weixin_tests


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 11, 21, 1, tzinfo=timezone.utc)
        self.agent = SimpleNamespace(inbox=AgentInbox(), record_subconscious_thought=AsyncMock())
        self.sink = AsyncMock()
        self.loop = self.make_loop()
        self.loop.record_interaction('weixin', 'owner@im.wechat', {})

    def make_loop(self):
        loop = SubconsciousLoop(self.agent, workspace=Path(self.tmp.name), interval_seconds=7200,
            delivery_sink=self.sink, deliverable_channels={'weixin'}, now_provider=lambda: self.now)
        loop._delivery_retry_delay_seconds = 0
        return loop

    def defer(self):
        return dict(action='defer', recipient='weixin:owner@im.wechat', subject='Answer the open question',
                    defer_reason='They asked to sleep until morning',
                    recheck_at=(self.now + timedelta(minutes=1)).isoformat(),
                    expires_at=(self.now + timedelta(days=1)).isoformat())

    async def cycle(self, decision, *, force=True):
        result = SubconsciousLoop._parse_subconscious_json(json.dumps({'note': None, 'decision': decision}))
        self.loop._generate_subconscious_thought = AsyncMock(return_value=result)
        if force:
            self.loop._next_think_at = 0
        await self.loop.maybe_think()

    async def pending(self):
        await self.cycle(self.defer())
        self.sink.assert_not_called()
        self.now += timedelta(minutes=2)
        return self.loop._followups.due(self.now.timestamp())[0]

    async def test_due_topic_survives_restart_and_wakes_before_regular_interval(self):
        topic = await self.pending()
        self.loop = self.make_loop()
        await self.cycle(dict(action='send', recipient=topic['recipient'], text='Here is the answer',
                              followup_id=topic['id']), force=False)
        self.sink.assert_awaited_once()
        self.assertEqual(self.loop._followups.due(self.now.timestamp()), [])
        self.assertEqual(self.loop._followups.outcomes()[0]['delivery_state'], 'accepted')

    async def test_frontend_answer_closes_topic_without_sending(self):
        topic = await self.pending()
        await self.cycle(dict(action='close', followup_id=topic['id'], reason='Already answered in chat'))
        self.sink.assert_not_called()
        self.assertFalse(self.loop._followups.due(self.now.timestamp()))

    async def test_interrupted_generation_retains_pending_topic(self):
        topic = await self.pending()
        async def generate(contacts):
            ticket = self.agent.inbox.reserve_turn()
            self.agent.inbox.discard(ticket)
            return {'note': 'stale', 'message': {'recipient': topic['recipient'], 'text': 'Old'},
                    'decision': dict(action='send', recipient=topic['recipient'], text='Old', followup_id=topic['id'])}
        self.loop._generate_subconscious_thought = generate
        await self.loop.maybe_think()
        self.sink.assert_not_called()
        self.agent.record_subconscious_thought.assert_not_called()
        self.now += timedelta(minutes=6)
        self.assertEqual(self.loop._followups.due(self.now.timestamp())[0]['id'], topic['id'])

    async def test_cannot_change_recipient_or_extend_expiry(self):
        topic = await self.pending()
        await self.cycle(dict(action='send', recipient='weixin:someone-else', text='Private', followup_id=topic['id']))
        self.sink.assert_not_called()
        self.now += timedelta(minutes=6)
        later = self.defer()
        later.update(followup_id=topic['id'], expires_at=(self.now + timedelta(days=5)).isoformat())
        await self.cycle(later)
        self.now += timedelta(days=2)
        self.assertFalse(self.loop._followups.due(self.now.timestamp()))

    async def test_invalid_followup_id_and_unknown_recipient_have_no_effect(self):
        await self.cycle(dict(action='close', followup_id='invented', reason='Done'))
        decision = self.defer()
        decision['recipient'] = 'weixin:unknown'
        await self.cycle(decision)
        self.assertFalse(self.loop._followups.path.exists())

    async def test_stale_claim_cannot_send_twice(self):
        topic = await self.pending()
        decision = dict(action='send', recipient=topic['recipient'], text='Answer', followup_id=topic['id'])
        self.loop._followups.apply(decision, topic, sending=True)
        with self.assertRaises(ValueError):
            self.loop._followups.apply(decision, topic, sending=True)
        self.assertFalse(self.make_loop()._followups.due(self.now.timestamp()))

    async def test_real_weixin_timeout_is_not_replayed(self):
        adapter, agent, client, _ = weixin_tests.WeixinAdapterTests()._adapter(self.tmp.name)
        adapter._context_tokens["owner@im.wechat"] = "ctx"
        async def lost_response(**kwargs):
            ticket = self.agent.inbox.reserve_turn()
            self.agent.inbox.discard(ticket)
            raise TimeoutError('response lost while user spoke')
        client.send_text_message = AsyncMock(side_effect=lost_response)
        self.loop._delivery_sink = adapter.deliver_subconscious_message
        await self.cycle(dict(action='send', recipient='weixin:owner@im.wechat', text='One message'))
        client.send_text_message.assert_awaited_once()
        self.assertEqual(self.loop._followups.outcomes()[0]['delivery_state'], 'unknown')

    async def test_acknowledged_first_chunk_is_not_replayed_after_second_chunk_times_out(self):
        adapter, agent, client, _ = weixin_tests.WeixinAdapterTests()._adapter(self.tmp.name, text_max_chars=4)
        adapter._context_tokens["owner@im.wechat"] = "ctx"
        client.send_text_message = AsyncMock(side_effect=[{}, TimeoutError('lost')])
        self.loop._delivery_sink = adapter.deliver_subconscious_message
        await self.cycle(dict(action='send', recipient='weixin:owner@im.wechat', text='abcdefgh'))
        self.assertEqual(client.send_text_message.await_count, 2)
        self.assertEqual(self.loop._followups.outcomes()[0]['delivery_state'], 'unknown')

    def test_protocol_validation(self):
        for decision in ({'action': 'bogus'}, {'action': 'send', 'recipient': 'x'},
                         dict(self.defer(), recheck_at='2026-11-21T03:00:00'),
                         dict(self.defer(), expires_at='2020-01-01T00:00:00+00:00')):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                SubconsciousLoop._parse_subconscious_json(json.dumps({'note': None, 'decision': decision}))
