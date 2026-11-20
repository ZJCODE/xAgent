"""Attribution must survive the same pipeline for direct, shared and inward experiences."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xagent.components.memory import RelationshipCard
from xagent.core.agent import Agent
from xagent.core.config import AgentConfig
from xagent.core.handlers.memory import MemoryHandler
from xagent.core.handlers.message import MessageHandler
from xagent.core.handlers.model import ModelStreamEvent
from xagent.core.journal import JournalLLMService
from xagent.core.runtime.subconscious import SubconsciousLoop, load_contacts
from xagent.core.working_context import WorkingContextCompactor, WorkingContextSummarizer
from xagent.schemas import Message, MessageType, RoleType


class ExperiencePerspectiveTests(unittest.IsolatedAsyncioTestCase):
    def test_one_perspective_is_used_by_all_model_roles(self):
        rules = AgentConfig.EXPERIENCE_PERSPECTIVE_RULES
        for prompt in (AgentConfig.BASE_AGENT_PROMPT, AgentConfig.DECISION_SYSTEM_PROMPT,
                       JournalLLMService.build_diary_system_prompt(),
                       JournalLLMService.build_summary_system_prompt('weekly'),
                       JournalLLMService.build_projection_system_prompt(),
                       WorkingContextSummarizer.build_system_prompt()):
            self.assertEqual(prompt.count(rules), 1)
        task = AgentConfig.build_subconscious_current_task()
        self.assertIn('your own experience', task)
        self.assertIn('even when they cannot currently be contacted', task)
        self.assertIn('privacy of everyone involved', task)

    async def test_outbound_audience_survives_history_diary_and_compaction(self):
        with tempfile.TemporaryDirectory() as root:
            agent = Agent(client=object(), workspace=root)
            messages = [await agent.message_handler.store_model_reply(
                reply_text='I will check the rollback.', sender_id='agent', channel='api', recipient_id='alice'),
                await agent.message_handler.store_model_reply(
                    reply_text='I will review this with you both.', sender_id='agent', channel='weixin',
                    recipient_id='engineering', room_name='Engineering')]
            loaded = await agent.message_storage.get_messages()
            self.assertEqual([m.recipient_id for m in loaded], ['alice', 'engineering'])
            for message in loaded:
                live_header = MessageHandler._format_transcript_message_header(message)
                self.assertIn(f'[to={message.recipient_id}]', live_header)
                for record in (MemoryHandler._experience_record(message),
                               WorkingContextCompactor._experience_record(message)):
                    transcript = JournalLLMService._format_transcript([record])
                    self.assertIn(f'[to={message.recipient_id}]', transcript)
                    if message.room_name:
                        self.assertIn('[room=Engineering]', transcript)
                        self.assertIn('[room=Engineering]', live_header)
            # No synthetic recipient is added to an old reply without one.
            unknown = Message(role=RoleType.ASSISTANT, sender_id='agent', channel='api', content='Old reply')
            self.assertNotIn('[to=', MessageHandler._format_transcript_message_header(unknown))
            self.assertNotIn('[to=', JournalLLMService._format_transcript([MemoryHandler._experience_record(unknown)]))

    def test_human_named_assistant_is_not_the_agent(self):
        human = Message(role=RoleType.USER, sender_id='assistant', channel='api', content='I prefer Chinese here.')
        for header in (MessageHandler._format_transcript_message_header(human),
                       JournalLLMService._format_transcript([MemoryHandler._experience_record(human)])):
            self.assertIn('[speaker=assistant]', header)
            self.assertNotIn('[speaker=ME]', header)

    def test_conflicting_recipient_metadata_does_not_attach_another_person_name(self):
        reply = Message(role=RoleType.ASSISTANT, recipient_id='bob', channel='api', content='An update',
                        metadata={'subconscious': {'source': 'subconscious', 'recipient': {
                            'user_id': 'alice', 'target': {'sender_name': 'Alice'}}}})
        for text in (MessageHandler._format_transcript_message_header(reply),
                     JournalLLMService._format_transcript([MemoryHandler._experience_record(reply)])):
            self.assertIn('[to=bob]', text)
            self.assertNotIn('[to=Alice', text)

    async def test_recent_people_are_understood_independently_of_delivery_availability(self):
        with tempfile.TemporaryDirectory() as root:
            agent = Agent(client=object(), workspace=root)
            for key, body in [('weixin:jun', 'I discussed a private appointment with this Jun.'),
                              ('api:jun', 'I discussed deployment with this other Jun.'),
                              ('api:unused', 'An unrelated acquaintance.')]:
                await agent.relationship_store.write_card(RelationshipCard(key=key, display_name='Jun', body=body))
            await agent.message_storage.add_messages([
                Message(role=RoleType.USER, sender_id='jun', channel='weixin', content='Keep my appointment private.', metadata={'sender_name': 'Jun'}),
                Message(role=RoleType.USER, sender_id='jun', channel='api', room_name='Engineering', content='Let us review deployment.', metadata={'sender_name': 'Jun'}),
                Message(role=RoleType.ENVIRONMENT, type=MessageType.CONTEXT_EVENT, sender_id='device', channel='api', content='Room became quiet.', metadata={'source': 'device'}),
            ])
            loop = SubconsciousLoop(agent, workspace=Path(root), deliverable_channels={'api'})
            loop.record_interaction('weixin', 'jun', {'sender_name': 'Jun'})
            loop.record_interaction('api', 'jun', {'sender_name': 'Jun'})
            loop.record_interaction('api', 'unused', {'sender_name': 'Jun'})
            contacts = loop._filter_deliverable_contacts(load_contacts(loop.contacts_file))
            with patch.object(AgentConfig, 'RELATIONSHIP_SUBCONSCIOUS_MAX_CARDS', 2):
                _, context = await loop._build_subconscious_turn_context(contacts)
            cards = next(m['content'] for m in context if m.get('name') == AgentConfig.SUBCONSCIOUS_RELATIONSHIPS_NAME)
            self.assertIn('[person: weixin:jun]', cards)
            self.assertIn('[person: api:jun]', cards)
            self.assertNotIn('[person: api:unused]', cards)
            addresses = context[-1]['content']
            targets = json.loads(addresses.split('\n', 1)[1])
            self.assertNotIn('weixin:jun', [t['recipient'] for t in targets])
            self.assertIn('exact identifier for the person you mean', addresses)
            self.assertIn('api:jun', [t['recipient'] for t in targets])

    async def test_synthetic_review_cases_build_with_the_production_context(self):
        from scripts.subconscious_review import SCENARIOS, review_scenario
        class Client:
            async def model_turn_events(self, **kwargs):
                yield ModelStreamEvent(type='text', delta='{"note": null, "message": null}')
        def make_agent(client, provider, workspace):
            agent = Agent(client=object(), workspace=workspace)
            agent.model_client = Client()
            return agent
        with patch('scripts.subconscious_review.make_agent', make_agent):
            for scenario in SCENARIOS:
                with self.subTest(scenario=scenario.name):
                    report = await review_scenario(None, {}, scenario)
                    self.assertTrue(all(report['checks'].values()))
