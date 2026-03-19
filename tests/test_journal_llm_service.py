import unittest

from xagent.components.memory.helper.llm_service import JournalLLMService


class JournalLLMServicePromptTests(unittest.TestCase):
    def test_diary_system_prompt_requires_explicit_speaker_attribution(self):
        prompt = JournalLLMService.build_diary_system_prompt(
            journal_date="2026-03-19",
            current_date="2026-03-19",
        )

        self.assertIn("Different users must stay clearly separated", prompt)
        self.assertIn("Every important fact must remain attributed to the speaker", prompt)
        self.assertIn('Prefer explicit attribution phrases such as "With abc, ...", "jun mentioned ...", or "T preferred ..."', prompt)
        self.assertIn("Never imply that different speakers shared the same preference, plan, event, or history", prompt)
        self.assertIn("If attribution is uncertain, keep that uncertainty", prompt)

    def test_summary_system_prompt_preserves_alias_locality_and_speaker_ownership(self):
        prompt = JournalLLMService.build_summary_system_prompt(
            period_type="weekly",
            period_label="2026-03-16 to 2026-03-22",
        )

        self.assertIn("Preserve speaker attribution throughout the summary", prompt)
        self.assertIn("summarize them separately or in clearly attributed clauses", prompt)
        self.assertIn("Preferences, plans, commitments, and experiences must stay attached to the speaker", prompt)
        self.assertIn('Generic labels such as "User A", "User B", "用户A", or "用户B" are local aliases inside a single source entry', prompt)
        self.assertIn("If the source material leaves attribution uncertain", prompt)


if __name__ == "__main__":
    unittest.main()