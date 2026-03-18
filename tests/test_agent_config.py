import unittest

from xagent.core.config import AgentConfig


class AgentConfigPromptTests(unittest.TestCase):
    def test_base_agent_prompt_includes_multi_user_boundaries(self):
        prompt = AgentConfig.BASE_AGENT_PROMPT

        self.assertIn("current speaker for this turn is identified in runtime context", prompt)
        self.assertIn("Treat every visible speaker label, sender_id, or user_id as a different person", prompt)
        self.assertIn("Never transfer one speaker's preferences, profile, plans, commitments, private facts, or emotional state to another speaker", prompt)
        self.assertIn("If speaker attribution is uncertain, say that it is uncertain and ask for clarification rather than guessing", prompt)
        self.assertIn("Retrieved journal entries may mention multiple speakers. Preserve their separation when reasoning", prompt)
        self.assertIn('Generic labels such as "User A", "User B", "用户A", or "用户B"', prompt)
        self.assertIn("answer only with information that can be attributed to the current speaker", prompt)


if __name__ == "__main__":
    unittest.main()
