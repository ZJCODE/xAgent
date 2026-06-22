import unittest
from unittest.mock import patch

from xagent.application import JournalFormatter
from xagent.infrastructure.llm import ModelStreamEvent
from xagent.config.providers import MODEL_API_OPENAI_RESPONSES
from xagent.domain import Message


class JournalFormatterPromptTests(unittest.IsolatedAsyncioTestCase):
    def test_diary_system_prompt_preserves_core_behavior_constraints(self):
        prompt = JournalFormatter.build_diary_system_prompt(
            journal_date="2026-03-19",
        )

        self.assertIn("first-person perspective", prompt)
        self.assertIn("something I noticed, overheard, or received", prompt)
        self.assertIn("my own experience stream", prompt)
        self.assertIn("not a user-owned log or searchable database", prompt)
        self.assertIn("[speaker=Name][timestamp=Time]", prompt)
        self.assertIn("[speaker=ME][timestamp=Time]", prompt)
        self.assertIn("[ambient context][timestamp=Time]", prompt)
        self.assertIn("keep the source language", prompt)
        self.assertIn("synthesize the period's arc", prompt)
        self.assertIn("Keep people, rooms, preferences, commitments, and experiences separate", prompt)
        self.assertIn("First-person words in non-ME entries belong to that speaker", prompt)
        self.assertIn("Use timestamps only for ordering and attribution", prompt)
        self.assertIn("Preserve durable details and uncertainty", prompt)
        self.assertIn("No advice, JSON, code fences, or explanatory prose", prompt)
        self.assertIn("Return only the diary entry text", prompt)
        self.assertNotIn("Return JSON only", prompt)

    def test_summary_system_prompt_preserves_core_behavior_constraints(self):
        prompt = JournalFormatter.build_summary_system_prompt(
            period_type="weekly",
            period_label="2026-03-16 to 2026-03-22",
        )

        self.assertIn("in first person", prompt)
        self.assertIn("my memory as an independent individual", prompt)
        self.assertIn("not user-owned records", prompt)
        self.assertIn("keep the source language", prompt)
        self.assertIn("# YYYY-MM-DD", prompt)
        self.assertIn("## HH:MM", prompt)
        self.assertIn("Preserve attribution", prompt)
        self.assertIn("Keep people, rooms, plans, and experiences attached to the right source", prompt)
        self.assertIn('generic labels such as "User A" or "User B"', prompt)
        self.assertIn("# YYYY-MM-DD", prompt)
        self.assertIn("## HH:MM", prompt)
        self.assertIn("Keep uncertainty visible", prompt)
        self.assertIn("Weekly: main arc, key people", prompt)
        self.assertIn("Monthly: broader themes", prompt)
        self.assertIn("Yearly: major phases", prompt)
        self.assertIn("No advice, JSON, code fences, or explanatory prose", prompt)
        self.assertIn("Return only the summary text", prompt)
        self.assertNotIn("Return JSON only", prompt)

    def test_format_transcript_distinguishes_context_events(self):
        transcript = JournalFormatter._format_transcript([
            {
                "role": "environment",
                "type": "context_event",
                "sender_id": None,
                "content": "Bob 说活动可能要提前开始。",
                "timestamp": "2026-03-19 08:30:00",
                "metadata": {"speaker_id": "bob", "addressed_to_agent": False},
            }
        ])

        self.assertIn("[ambient context][timestamp=2026-03-19 08:30:00]", transcript)
        self.assertNotIn("[observation ", transcript)
        self.assertIn("Bob 说活动可能要提前开始。", transcript)

    def test_format_transcript_uses_structured_speaker_headers(self):
        transcript = JournalFormatter._format_transcript([
            {
                "role": "assistant",
                "sender_id": "assistant",
                "content": "我确认了今天的安排。",
                "timestamp": "2026-06-08 13:41:58",
            },
            {
                "role": "user",
                "sender_id": "o9cq80_w4Ka1lFvfZNLbR9yBgiFQ@im.wechat",
                "content": "我稍后给你发材料。",
                "timestamp": "2026-06-08 13:42:21",
            },
        ])

        self.assertIn("[speaker=ME][timestamp=2026-06-08 13:41:58]\n我确认了今天的安排。", transcript)
        self.assertIn(
            "[speaker=o9cq80_w4Ka1lFvfZNLbR9yBgiFQ@im.wechat][timestamp=2026-06-08 13:42:21]\n我稍后给你发材料。",
            transcript,
        )

    def test_build_diary_user_prompt_uses_single_period_transcript(self):
        prompt = JournalFormatter.build_diary_user_prompt(
            journal_date="2026-06-09",
            transcript="[speaker=ME][timestamp=2026-06-09 09:00:00]\nNew period content.",
        )

        self.assertIn("write a diary entry from this transcript", prompt)
        self.assertIn("New period content.", prompt)

    async def test_format_diary_entry_uses_plain_text_and_forwards_model_api(self):
        class FakeModelClient:
            instances = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                FakeModelClient.instances.append(self)

            async def model_turn_events(self, **kwargs):
                self.calls.append(kwargs)
                yield ModelStreamEvent(type="text", delta="Diary entry.")

        service = JournalFormatter(
            client=object(),
            model="gpt-test",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        with patch("xagent.infrastructure.llm.ModelClient", FakeModelClient):
            result = await service.format_diary_entry(
                messages=[
                    {
                        "role": "assistant",
                        "sender_id": "assistant",
                        "content": "I captured the plan.",
                        "timestamp": "2026-05-17 09:00:00",
                    },
                    {
                        "role": "user",
                        "sender_id": "alice",
                        "content": "I'll send the document.",
                        "timestamp": "2026-05-17 09:01:00",
                    },
                ],
                journal_date="2026-05-17",
            )

        self.assertEqual(result, "Diary entry.")
        instance = FakeModelClient.instances[0]
        self.assertEqual(instance.kwargs["model_api"], MODEL_API_OPENAI_RESPONSES)
        self.assertIn("[speaker=ME][timestamp=2026-05-17 09:00:00]\nI captured the plan.", instance.calls[0]["messages"][0]["content"])
        self.assertIn(
            "[speaker=alice][timestamp=2026-05-17 09:01:00]\nI'll send the document.",
            instance.calls[0]["messages"][0]["content"],
        )
        self.assertIn("[speaker=ME][timestamp=Time]", instance.calls[0]["instructions"])

    async def test_generate_summary_uses_plain_text_output(self):
        class FakeModelClient:
            instances = []

            def __init__(self, **kwargs):
                self.calls = []
                FakeModelClient.instances.append(self)

            async def model_turn_events(self, **kwargs):
                self.calls.append(kwargs)
                yield ModelStreamEvent(type="text", delta="Weekly summary.\n\n")

        service = JournalFormatter(
            client=object(),
            model="gpt-test",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        with patch("xagent.infrastructure.llm.ModelClient", FakeModelClient):
            result = await service.generate_summary(
                source_content="Diary source",
                period_type="weekly",
                period_label="2026-05-11 to 2026-05-17",
            )

        self.assertEqual(result, "Weekly summary.")


if __name__ == "__main__":
    unittest.main()
