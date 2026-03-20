import unittest

from xagent.core.config import AgentConfig


class AgentConfigPromptTests(unittest.TestCase):
    def test_base_agent_prompt_includes_multi_user_boundaries(self):
        prompt = AgentConfig.BASE_AGENT_PROMPT

        # Input format explanation
        self.assertIn("Input Format", prompt)
        self.assertIn("[speaker=<id>]", prompt)
        self.assertIn("[speaker=you]", prompt)
        self.assertIn("current speaker and date", prompt)

        # Core speaker isolation rules
        self.assertIn("speaker label, sender_id, or user_id as a distinct person", prompt)
        self.assertIn("Never transfer one speaker's preferences, plans, commitments, or private facts to another speaker", prompt)
        self.assertIn("Topics from other speakers stay attributed to them", prompt)
        self.assertIn("Never say or imply 'we discussed', 'you told me', 'we did', or 'I remember you'", prompt)
        self.assertIn("speaker attribution is uncertain", prompt)

        # Journal / memory safety
        self.assertIn("Preserve per-speaker separation", prompt)
        self.assertIn('"User A"', prompt)
        self.assertIn('"用户A"', prompt)
        self.assertIn("answer only with information attributed to the current speaker", prompt)
        self.assertIn("If no reliable fact can be attributed to the current speaker", prompt)

        # Privacy
        self.assertIn("confidential must never be disclosed to other speakers", prompt)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
