import unittest
from unittest.mock import patch

from xagent.components.memory.helper.llm_service import JournalLLMService


class JournalLLMServicePromptTests(unittest.TestCase):
    def make_service(self) -> JournalLLMService:
        with patch("xagent.components.memory.helper.llm_service.AsyncOpenAI"):
            return JournalLLMService()

    def test_rewrite_system_prompt_contains_guidance_and_rules(self):
        service = self.make_service()

        prompt = service._build_rewrite_system_prompt(
            current_date="2026-03-18",
            journal_date="2026-03-18",
        )

        self.assertIn("Write in first person", prompt)
        self.assertIn('Refer to the observer as "I"', prompt)
        self.assertIn('Any "agent", "assistant", or "AI" speaker in the transcript refers to me', prompt)
        self.assertIn("Do not describe the day as if I were merely watching a transcript from the outside", prompt)
        self.assertIn("Different users must stay clearly separated", prompt)
        self.assertIn('"User A" or "User B"', prompt)
        self.assertIn("Headings are optional", prompt)
        self.assertIn("main movement of the day", prompt)
        self.assertIn("who appeared and what each person was doing", prompt)
        self.assertIn("notable preferences or expressions or changes", prompt)
        self.assertIn("Do not give advice, proposals, next steps, reminders, recommendations", prompt)
        self.assertIn("Do not end with offers to help", prompt)
        self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in prompt))

    def test_rewrite_user_prompt_treats_existing_journal_as_observation_draft(self):
        service = self.make_service()

        prompt = service._build_rewrite_user_prompt(
            existing_journal="existing draft",
            new_transcript="[2026-03-18 10:00:00] User user_a: keep going",
            journal_date="2026-03-18",
        )

        self.assertIn("existing journal is today's earlier observation draft", prompt.lower())
        self.assertIn("new transcript is today's newly arrived interaction fragment", prompt.lower())
        self.assertIn("Speaker labels already shown in the transcript are the names you should use", prompt)
        self.assertIn('using "I" when referring to the narrator', prompt)
        self.assertIn('Treat any "agent", "assistant", or "AI" role inside the transcript as me', prompt)
        self.assertIn("Do not let the narrator call itself “Assistant”", prompt)
        self.assertIn("Do not turn it into advice, a plan, a suggestion, a recommendation", prompt)
        self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in prompt))

    def test_normalize_journal_content_preserves_multisection_layout(self):
        normalized = JournalLLMService._normalize_journal_content(
            """
            今天发生了什么
              今天整体围绕路线图展开。


            有哪些人，他们大致在做什么
                alice 继续推进发布节奏。
            """
        )

        self.assertIn("今天发生了什么\n今天整体围绕路线图展开。", normalized)
        self.assertIn("\n\n有哪些人，他们大致在做什么\nalice 继续推进发布节奏。", normalized)


if __name__ == "__main__":
    unittest.main()
