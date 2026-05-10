import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from xagent.core.config import AgentConfig
from xagent.interfaces.base import BaseAgentRunner


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


class ProviderConfigTests(unittest.TestCase):
    def test_provider_config_builds_openai_compatible_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "agent.yaml"
            config_path.write_text(
                """
agent:
  name: "ProviderAgent"
  model: "deepseek-v4-pro"
  workspace: "{workspace}"
  provider:
    base_url: "https://api.deepseek.com"
    api_key_env: "DEEPSEEK_API_KEY"
  capabilities:
    tools: []
server:
  host: "127.0.0.1"
  port: 8010
""".format(workspace=tmpdir),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
                runner = BaseAgentRunner(config_path=str(config_path))

            self.assertEqual(runner.agent.model, "deepseek-v4-pro")
            self.assertEqual(str(runner.agent.client.base_url).rstrip("/"), "https://api.deepseek.com")


if __name__ == "__main__":
    unittest.main()
