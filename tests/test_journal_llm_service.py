import unittest
from unittest.mock import patch

from xagent.components.memory import JournalLLMService
from xagent.core.config import ReplyType
from xagent.core.providers import MODEL_API_OPENAI_RESPONSES
from xagent.schemas import Message


class JournalLLMServicePromptTests(unittest.IsolatedAsyncioTestCase):
    def test_diary_system_prompt_preserves_core_behavior_constraints(self):
        prompt = JournalLLMService.build_diary_system_prompt(
            journal_date="2026-03-19",
            current_date="2026-03-19",
        )

        self.assertIn("first-person perspective", prompt)
        self.assertIn("observations, overheard speech, notifications, reminders", prompt)
        self.assertIn("Keep the source language and do not translate", prompt)
        self.assertIn("Synthesize important points instead of replaying the transcript line by line", prompt)
        self.assertIn("Keep different people separate", prompt)
        self.assertIn("Attribute important facts to the speaker or source", prompt)
        self.assertIn("I overheard", prompt)
        self.assertIn("If attribution is uncertain, keep the uncertainty visible", prompt)
        self.assertIn("do not give advice, proposals, recommendations, next steps", prompt)
        self.assertIn("Return only the diary entry text", prompt)
        self.assertNotIn("Return JSON only", prompt)

    def test_summary_system_prompt_preserves_core_behavior_constraints(self):
        prompt = JournalLLMService.build_summary_system_prompt(
            period_type="weekly",
            period_label="2026-03-16 to 2026-03-22",
        )

        self.assertIn("first-person perspective", prompt)
        self.assertIn("keep the source language and do not translate", prompt)
        self.assertIn("Preserve speaker attribution", prompt)
        self.assertIn("Do not flatten multiple people into one undifferentiated narrative", prompt)
        self.assertIn("Keep each person's preferences, plans, commitments, and experiences attached to that person", prompt)
        self.assertIn('generic labels such as "User A", "User B", "用户A", or "用户B"', prompt)
        self.assertIn("If attribution is uncertain, keep the uncertainty visible", prompt)
        self.assertIn("Weekly: main arc, key people", prompt)
        self.assertIn("Monthly: broader themes", prompt)
        self.assertIn("Yearly: major phases", prompt)
        self.assertIn("not advice; do not give recommendations or next steps", prompt)
        self.assertIn("Return only the summary text", prompt)
        self.assertNotIn("Return JSON only", prompt)

    def test_format_transcript_distinguishes_context_events(self):
        event = Message.create_context_event(
            "Bob 说活动可能要提前开始。",
            source="microphone",
            event_type="overheard_speech",
            metadata={"speaker_id": "bob", "addressed_to_agent": False},
        )

        transcript = JournalLLMService._format_transcript([
            {
                "role": event.role.value,
                "type": event.type.value,
                "sender_id": event.sender_id,
                "content": event.content,
                "metadata": event.metadata,
            }
        ])

        self.assertIn("[ambient context]: Bob 说活动可能要提前开始。", transcript)
        self.assertNotIn("[observation ", transcript)
        self.assertIn("Bob 说活动可能要提前开始。", transcript)
        self.assertIsNone(event.sender_id)

    async def test_format_diary_entry_uses_plain_text_and_forwards_model_api(self):
        class FakeModelClient:
            instances = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                FakeModelClient.instances.append(self)

            async def call(self, **kwargs):
                self.calls.append(kwargs)
                return ReplyType.SIMPLE_REPLY, "Diary entry."

        service = JournalLLMService(
            client=object(),
            model="gpt-test",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        with patch("xagent.core.handlers.model.ModelClient", FakeModelClient):
            result = await service.format_diary_entry(
                messages=[{"role": "user", "sender_id": "alice", "content": "hello"}],
                journal_date="2026-05-17",
            )

        self.assertEqual(result, "Diary entry.")
        instance = FakeModelClient.instances[0]
        self.assertEqual(instance.kwargs["model_api"], MODEL_API_OPENAI_RESPONSES)
        self.assertIsNone(instance.calls[0]["output_type"])

    async def test_generate_summary_uses_plain_text_output(self):
        class FakeModelClient:
            instances = []

            def __init__(self, **kwargs):
                self.calls = []
                FakeModelClient.instances.append(self)

            async def call(self, **kwargs):
                self.calls.append(kwargs)
                return ReplyType.SIMPLE_REPLY, "Weekly summary.\n\n"

        service = JournalLLMService(
            client=object(),
            model="gpt-test",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        with patch("xagent.core.handlers.model.ModelClient", FakeModelClient):
            result = await service.generate_summary(
                source_content="Diary source",
                period_type="weekly",
                period_label="2026-05-11 to 2026-05-17",
            )

        self.assertEqual(result, "Weekly summary.")
        self.assertIsNone(FakeModelClient.instances[0].calls[0]["output_type"])


if __name__ == "__main__":
    unittest.main()
