import unittest
from unittest.mock import patch

from xagent.core.journal import JournalLLMService
from xagent.core.config import ReplyType
from xagent.core.prompts import PromptAssembler
from xagent.core.providers import MODEL_API_OPENAI_RESPONSES, ReasoningConfig
from xagent.schemas import Message


class JournalLLMServicePromptTests(unittest.IsolatedAsyncioTestCase):
    def test_diary_system_prompt_preserves_core_behavior_constraints(self):
        prompt = PromptAssembler.DIARY_CONTRACT

        self.assertIn("first-person diary prose", prompt)
        self.assertIn("ME means me", prompt)
        self.assertIn("other speakers own their words", prompt)
        self.assertIn("people, rooms, durable facts and uncertainty", prompt)
        self.assertIn("100-500 characters", prompt)
        self.assertLess(len(prompt), 600)

    def test_summary_system_prompt_preserves_core_behavior_constraints(self):
        prompt = PromptAssembler.SUMMARY_CONTRACT

        self.assertIn("in first person", prompt)
        self.assertIn("Preserve attribution", prompt)
        self.assertIn("people, rooms, decisions and chronology", prompt)
        self.assertIn("Return only body text", prompt)
        self.assertLess(len(prompt), 500)

    def test_format_transcript_distinguishes_context_events(self):
        transcript = JournalLLMService._format_transcript([
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
        transcript = JournalLLMService._format_transcript([
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
        prompt = PromptAssembler.diary_task(
            transcript="[speaker=ME][timestamp=2026-06-09 09:00:00]\nNew period content.",
            journal_date="2026-06-09",
        )

        self.assertIn("date=2026-06-09", prompt)
        self.assertIn("storage layer adds headings", prompt)
        self.assertIn("New period content.", prompt)

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
            reasoning=ReasoningConfig(enabled=True, effort="medium"),
        )

        with patch("xagent.core.handlers.model.ModelClient", FakeModelClient):
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
        self.assertEqual(instance.kwargs["reasoning"], ReasoningConfig(enabled=True, effort="medium"))
        self.assertIn("[speaker=ME][timestamp=2026-05-17 09:00:00]\nI captured the plan.", instance.calls[0]["messages"][0]["content"])
        self.assertIn(
            "[speaker=alice][timestamp=2026-05-17 09:01:00]\nI'll send the document.",
            instance.calls[0]["messages"][0]["content"],
        )
        self.assertIn("ME means me", instance.calls[0]["instructions"])
        self.assertIn("100-500 characters", instance.calls[0]["instructions"])
        self.assertIn("storage layer adds headings", instance.calls[0]["messages"][0]["content"])
        self.assertNotIn("[internal_monologue]", instance.calls[0]["instructions"])

    async def test_format_diary_entry_raises_instead_of_returning_raw_transcript_on_model_error(self):
        class FakeModelClient:
            async def call(self, **kwargs):
                raise RuntimeError("model unavailable")

        service = JournalLLMService(
            client=object(),
            model="gpt-test",
            model_api=MODEL_API_OPENAI_RESPONSES,
        )

        with patch("xagent.core.handlers.model.ModelClient", lambda **kwargs: FakeModelClient()):
            with self.assertRaises(RuntimeError):
                await service.format_diary_entry(
                    messages=[
                        {
                            "role": "user",
                            "sender_id": "alice",
                            "content": "raw source should not be used as diary fallback",
                            "timestamp": "2026-05-17 09:01:00",
                        }
                    ],
                    journal_date="2026-05-17",
                )

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


if __name__ == "__main__":
    unittest.main()
