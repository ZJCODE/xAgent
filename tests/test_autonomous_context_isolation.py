"""Internal turn data follows tool-process lifetime, never conversation lifetime."""
import json
import tempfile
import unittest
from pathlib import Path

from tests import test_agent_inbox as agent_fixtures
from tests import test_memory_handler as memory_fixtures
from tests.test_agent_chat_flow import CapturingModelClient, FakeToolCall, FakeToolExecutor
from xagent.components.message import MessageStorage
from xagent.components.memory import MarkdownMemory
from xagent.core.config import AgentConfig, ReplyType
from xagent.core.handlers.memory import MemoryHandler
from xagent.core.handlers.message import MessageHandler
from xagent.core.runtime.autonomous import AutonomousWake
from xagent.core.runtime.subconscious import SubconsciousLoop
from xagent.core.working_context import WorkingContextCompactor, WorkingContextStore
from xagent.schemas import Message, RoleType


def internal(index=0):
    message = Message.create(f'INTERNAL_SENTINEL_{index}', role=RoleType.USER, sender_id='__autonomous__')
    if index % 3 == 0:
        message.channel = 'autonomous'
    elif index % 3 == 1:
        message.metadata['source'] = 'autonomous'
    else:
        message.metadata['inbox_kind'] = 'autonomous_turn'
    return message


def human(text):
    message = Message.create(text, role=RoleType.USER, sender_id='alice')
    message.channel = 'api'
    return message


class AutonomousContextIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = MessageStorage(str(self.root / 'messages.sqlite3'))

    async def test_legacy_rows_filtered_before_pagination_and_search(self):
        await self.storage.add_messages([human('one'), *[internal(i) for i in range(100)], human('two'), human('three')])
        self.assertEqual([m.content for m in await self.storage.get_messages(2)], ['two', 'three'])
        self.assertEqual([m.content for m in await self.storage.get_messages(2, offset=1)], ['one', 'two'])
        self.assertEqual(await self.storage.get_message_count(), 3)
        self.assertEqual(len(await self.storage.get_messages_in_cursor_range()), 3)
        self.assertEqual(await self.storage.search_messages(['INTERNAL_SENTINEL']), '')
        # A user quoting logs is actual dialogue and must remain visible.
        await self.storage.add_messages(human('Please explain INTERNAL_SENTINEL and channel=autonomous'))
        self.assertIn('Please explain', await self.storage.search_messages(['INTERNAL_SENTINEL']))
        self.assertEqual(await self.storage.get_message_count(), 4)

    async def test_generic_history_filter_fills_real_message_window(self):
        storage = memory_fixtures._FakeMessageStorage([human('one'), human('two'), *[internal(i) for i in range(100)], human('three')])
        handler = MessageHandler(storage)
        messages = await handler.get_recent_messages(3)
        self.assertEqual([m.content for m in messages], ['one', 'two', 'three'])

    async def test_autonomous_execution_is_transient_but_receipts_and_actual_reply_remain(self):
        final = json.dumps(dict(outcome='completed', summary='INTERNAL_SENTINEL result',
            next_step=None, blocked_on=None, note=None, message=None))
        model = CapturingModelClient([(ReplyType.TOOL_CALL, [FakeToolCall()]),
            (ReplyType.SIMPLE_REPLY, final), (ReplyType.SIMPLE_REPLY, 'Hello Alice')])
        executor = FakeToolExecutor()
        agent = agent_fixtures.AgentInboxTests()._build_agent(self.storage, model, tool_executor=executor)
        agent.subconscious_mode = 'act'
        loop = SubconsciousLoop(agent, workspace=self.root, interval_seconds=60)
        wake = AutonomousWake(loop)
        wake.state['last_result'] = {'summary': 'INTERNAL_SENTINEL prior state'}
        wake.state['next_run_at'] = 0
        loop._autonomous = wake
        await loop.maybe_think()
        self.assertEqual(len(executor.seen_input_messages), 1)
        self.assertIn('INTERNAL_SENTINEL prior state', str(model.calls[0]))
        self.assertEqual(await self.storage.get_message_count(), 0)
        with self.storage._connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        receipt = (await agent.turn_store.list_records())[0]
        self.assertIn('INTERNAL_SENTINEL prior state', receipt['internal_input'])
        self.assertEqual(receipt['autonomous_result']['outcome'], 'completed')
        async for _ in agent.chat_events('hello', user_id='alice', channel='api'):
            pass
        self.assertNotIn('INTERNAL_SENTINEL', str(model.calls[-1]))
        self.assertEqual(await self.storage.get_message_count(), 2)
        await agent.message_handler.store_model_reply('Actual completion notification', 'agent',
            channel='api', recipient_id='alice', metadata={'subconscious': {'source': 'subconscious'}})
        self.assertEqual((await self.storage.get_messages(1))[0].content, 'Actual completion notification')

    async def test_legacy_summary_is_rebuilt_with_clean_cursor_coverage(self):
        await self.storage.add_messages([human('fact-0'), human('fact-1'), *[internal(i) for i in range(100)],
            *[human(f'fact-{i}') for i in range(2, 8)]])
        path = self.root / 'working.json'
        old = json.dumps({'summary': 'INTERNAL_SENTINEL polluted summary', 'covers_through_cursor': 105})
        path.write_text(old)
        store = WorkingContextStore(path)
        self.assertEqual(store.read().summary, '')
        self.assertEqual(store.read().covers_through_cursor, 0)
        calls = []
        class Summarizer:
            async def summarize(self, *, previous_summary, records):
                calls.append((previous_summary, records))
                return '; '.join(r['content'] for r in records)
        compactor = WorkingContextCompactor(store=store, message_storage=self.storage,
            summarizer=Summarizer(), hot_window=3, roll_slack=1)
        view = await compactor.ensure_fresh()
        self.assertEqual(calls[0][0], '')
        self.assertNotIn('INTERNAL_SENTINEL', str(calls))
        self.assertEqual(view.covers_through_cursor, 105)
        recent = await self.storage.get_messages(12)
        raw, _ = MessageHandler._budget_transcript_entries(recent, 3, view.covers_through_cursor)
        self.assertEqual([text for _, text in raw], ['fact-5', 'fact-6', 'fact-7'])
        for i in range(5):
            self.assertIn(f'fact-{i}', view.summary)
        self.assertEqual(path.with_name(path.name + '.legacy').read_text(), old)
        self.assertEqual(store.read().summary, view.summary)

    async def test_empty_filtered_diary_window_does_not_skip_later_real_messages(self):
        messages = [*[internal(i) for i in range(20)], *[human(f'important-{i}') for i in range(20)],
                    *[internal(i) for i in range(60)]]
        storage = memory_fixtures._FakeMessageStorage(messages)
        llm = memory_fixtures._FakeLLMService()
        handler = MemoryHandler(memory=MarkdownMemory(str(self.root / 'memory')),
            message_storage=storage, llm_service=llm, diary_write_batch=20)
        await handler.run_maintenance(force=True)
        self.assertEqual(handler._last_processed_message_id, 20)
        await handler.run_maintenance(force=True)
        self.assertTrue(llm.diary_calls)
        self.assertIn('important-0', str(llm.diary_calls))
        self.assertNotIn('INTERNAL_SENTINEL', str(llm.diary_calls))

    async def test_diary_and_identity_projection_exclude_internal_records(self):
        messages = [human('remember this'), internal()]
        messages[1].metadata.update(memory_policy='always', memory_worthy=True)
        storage = memory_fixtures._FakeMessageStorage(messages)
        llm = memory_fixtures._FakeLLMService()
        handler = MemoryHandler(memory=MarkdownMemory(str(self.root / 'memory')),
            message_storage=storage, llm_service=llm, diary_write_batch=20)
        await handler.run_maintenance(force=True)
        self.assertTrue(llm.diary_calls)
        self.assertNotIn('INTERNAL_SENTINEL', str(llm.diary_calls))
        self.assertFalse(handler._is_memory_worthy_experience(messages[1]))
        self.assertNotIn('__autonomous__', str(handler._extract_participants(messages)))
