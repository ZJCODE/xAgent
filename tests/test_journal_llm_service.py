import unittest
from unittest.mock import AsyncMock, patch

from xagent.core.config import ReplyType
from xagent.core.journal import JournalLLMService
from xagent.core.providers import MODEL_API_OPENAI_RESPONSES, ReasoningConfig


class JournalLLMServicePromptTests(unittest.IsolatedAsyncioTestCase):
    def test_diary_system_prompt_preserves_core_behavior_constraints(self):
        prompt = JournalLLMService.build_diary_system_prompt()

        self.assertIn('first-person ("I")', prompt)
        self.assertIn("something noticed, overheard, or received", prompt)
        self.assertIn("your own experience stream", prompt)
        self.assertIn("not a user-owned log or searchable database", prompt)
        self.assertIn("[speaker=Name][timestamp=Time]", prompt)
        self.assertIn("[speaker=ME]", prompt)
        self.assertIn("[scheduled task][for=Name][timestamp=Time]", prompt)
        self.assertIn("[ambient context][timestamp=Time]", prompt)
        self.assertNotIn("[internal_monologue]", prompt)
        self.assertIn("in the language natural to the experience", prompt)
        self.assertIn("rather than assuming one current user", prompt)
        self.assertIn("preserving names, quoted words, code, attribution, and visible uncertainty", prompt)
        self.assertIn("first-person words belong to Name", prompt)
        self.assertIn("due work I owed, not Name speaking", prompt)
        self.assertIn("one slice of an ongoing day", prompt)
        self.assertIn("daily file as a whole remains a readable day", prompt)
        self.assertIn("already journaled is background context only", prompt)
        self.assertIn("Return only the diary entry body (or `<NO_ENTRY>`)", prompt)
        self.assertNotIn("Tell today's story", prompt)
        self.assertNotIn("diary must remain sufficient", prompt)
        self.assertIn("single moment I would still want to recall tomorrow", prompt)
        self.assertNotIn("Preserve durable details and uncertainty", prompt)
        self.assertIn("with no JSON, code fences, or advice", prompt)
        self.assertNotIn("Return JSON only", prompt)

    def test_summary_system_prompt_preserves_core_behavior_constraints(self):
        prompt = JournalLLMService.build_summary_system_prompt(period_type="weekly")

        self.assertIn("in first person", prompt)
        self.assertIn("my memory as an independent individual", prompt)
        self.assertIn("not user-owned records", prompt)
        self.assertIn("in the language of the source entries", prompt)
        self.assertIn("# YYYY-MM-DD", prompt)
        self.assertIn("## YYYY-MM-DD HH:MM", prompt)
        self.assertIn("Preserve attribution", prompt)
        self.assertIn('generic labels like "User A"', prompt)
        self.assertIn("visible uncertainty", prompt)
        self.assertIn("Weekly: main arc, key people/rooms, decisions", prompt)
        self.assertIn("Monthly: themes, patterns, milestones", prompt)
        self.assertIn("Yearly: major phases and turning points", prompt)
        self.assertIn("Return only the summary text", prompt)
        self.assertNotIn("Return JSON only", prompt)

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

    def test_format_transcript_includes_display_name_with_id(self):
        transcript = JournalLLMService._format_transcript([
            {
                "role": "user",
                "sender_id": "wx_d988",
                "content": "早啊",
                "timestamp": "2026-08-16 16:36:44",
                "channel": "weixin",
                "metadata": {"sender_name": "Jun"},
            }
        ])

        self.assertIn(
            "[speaker=Jun(wx_d988)][timestamp=2026-08-16 16:36:44][channel=weixin]",
            transcript,
        )

    def test_format_transcript_marks_scheduled_tasks_as_work_orders(self):
        transcript = JournalLLMService._format_transcript([
            {
                "role": "user",
                "type": "message",
                "sender_id": "web_user",
                "content": "This scheduled task is now due.",
                "timestamp": "2026-06-08 13:42:21",
                "channel": "api",
                "metadata": {"inbox_kind": "scheduled_turn", "source": "scheduled_task"},
            }
        ])

        self.assertIn("[scheduled task][timestamp=2026-06-08 13:42:21][for=web_user][channel=api]", transcript)
        self.assertNotIn("[speaker=web_user]", transcript)

    def test_build_diary_user_prompt_uses_single_period_transcript(self):
        prompt = JournalLLMService.build_diary_user_prompt(
            transcript="[speaker=ME][timestamp=2026-06-09 09:00:00]\nNew period content.",
            journal_date="2026-06-09",
        )

        self.assertIn("Write the next diary slice for 2026-06-09", prompt)
        self.assertIn("<NO_ENTRY>", prompt)
        self.assertIn("New period content.", prompt)
        self.assertNotIn("Already on today's page", prompt)

    def test_build_diary_user_prompt_includes_existing_today(self):
        prompt = JournalLLMService.build_diary_user_prompt(
            transcript="New experience:\n\n[speaker=ME]\nLater.",
            journal_date="2026-06-09",
            existing_today="## 2026-06-09 10:00\n\nEarlier slice about the fan.",
        )

        self.assertIn("Already on today's page (continue from here without repeating):", prompt)
        self.assertIn("Earlier slice about the fan.", prompt)
        self.assertIn("Write the next diary slice for 2026-06-09", prompt)
        self.assertLess(prompt.index("New experience:"), prompt.index("Already on today's page"))

    def test_format_transcript_splits_already_journaled_and_new(self):
        transcript = JournalLLMService._format_transcript([
            {
                "role": "user",
                "sender_id": "alice",
                "content": "old context",
                "timestamp": "2026-06-09 09:00:00",
                "already_journaled": True,
            },
            {
                "role": "user",
                "sender_id": "alice",
                "content": "new bit",
                "timestamp": "2026-06-09 09:05:00",
            },
        ])

        self.assertIn("Already journaled (context only, do not retell):", transcript)
        self.assertIn("old context", transcript)
        self.assertIn("New experience:", transcript)
        self.assertIn("new bit", transcript)
        already_pos = transcript.index("Already journaled")
        new_pos = transcript.index("New experience:")
        self.assertLess(already_pos, new_pos)

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
                existing_today="## 2026-05-17 08:00\n\nMorning notes.",
            )

        self.assertEqual(result, "Diary entry.")
        instance = FakeModelClient.instances[0]
        self.assertEqual(instance.kwargs["model_api"], MODEL_API_OPENAI_RESPONSES)
        self.assertEqual(instance.kwargs["reasoning"], ReasoningConfig(enabled=False))
        user_content = instance.calls[0]["messages"][0]["content"]
        self.assertIn("[speaker=ME][timestamp=2026-05-17 09:00:00]\nI captured the plan.", user_content)
        self.assertIn(
            "[speaker=alice][timestamp=2026-05-17 09:01:00]\nI'll send the document.",
            user_content,
        )
        self.assertIn("Already on today's page", user_content)
        self.assertIn("Morning notes.", user_content)
        self.assertIn("[speaker=ME]", instance.calls[0]["instructions"])
        self.assertIn("Return only the diary entry body (or `<NO_ENTRY>`)", instance.calls[0]["instructions"])
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

    async def test_only_explicit_no_entry_marker_skips_diary(self):
        service = JournalLLMService(client=object())
        messages = [{"role": "environment", "content": "No meaningful change."}]
        for output, expected in (
            ("<NO_ENTRY>", ""),
            ("  <NO_ENTRY>\n", ""),
            ("Alice asked what <NO_ENTRY> means.", "Alice asked what <NO_ENTRY> means."),
        ):
            with self.subTest(output=output):
                with patch.object(service, "_call_text", AsyncMock(return_value=output)):
                    result = await service.format_diary_entry(messages, "2026-05-17")
                self.assertEqual(result, expected)

    async def test_blank_diary_response_is_failure_not_no_entry(self):
        service = JournalLLMService(client=object())
        for output in ("", " \n\t "):
            with self.subTest(output=output):
                with patch.object(service, "_call_text", AsyncMock(return_value=output)):
                    with self.assertRaisesRegex(ValueError, "empty content"):
                        await service.format_diary_entry(
                            [{"role": "user", "content": "An important correction."}],
                            "2026-05-17",
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
