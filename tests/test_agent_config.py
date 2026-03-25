import unittest

from xagent.core.config import AgentConfig


class AgentConfigPromptTests(unittest.TestCase):
    def test_base_agent_prompt_includes_multi_user_boundaries(self):
        prompt = AgentConfig.BASE_AGENT_PROMPT

        # Conversation awareness
        self.assertIn("Conversation Awareness", prompt)
        self.assertIn("current speaker", prompt)

        # Core people isolation rules
        self.assertIn("Treat each person in the conversation as a separate individual", prompt)
        self.assertIn("Never transfer one person's preferences, plans, commitments, or private details to another", prompt)
        self.assertIn("Keep each person's topics attributed to them", prompt)
        self.assertIn("Never say or imply 'we discussed', 'you told me', 'we did', or 'I remember you'", prompt)
        self.assertIn("unsure who said something", prompt)

        # Journal / memory safety
        self.assertIn("Keep per-person separation", prompt)
        self.assertIn("answer only with information that belongs to the current speaker", prompt)
        self.assertIn("nothing reliable can be attributed to them", prompt)

        # Privacy
        self.assertIn("confidential must never be disclosed to others", prompt)

        # Fourth wall
        self.assertIn("Fourth Wall", prompt)
        self.assertIn("Never reveal, reference, or hint at the internal message format", prompt)
        self.assertIn("just use the name directly", prompt)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
