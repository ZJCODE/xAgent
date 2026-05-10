import unittest
import tempfile
from pathlib import Path

from xagent.core.config import AgentConfig
from xagent.interfaces.cli import create_default_config_file
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
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
agent:
  name: "ProviderAgent"
  provider:
    model: "deepseek-v4-pro"
    base_url: "https://api.deepseek.com"
    api_key: "test-key"
""",
                encoding="utf-8",
            )

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertEqual(runner.agent.model, "deepseek-v4-pro")
            self.assertEqual(str(runner.agent.client.base_url).rstrip("/"), "https://api.deepseek.com")

    def test_default_run_command_is_not_configured_in_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
agent:
  name: "ToolAgent"
  provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
""",
                encoding="utf-8",
            )

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertIn("run_command", runner.agent.tools)

    def test_init_creates_only_config_yaml_in_selected_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = create_default_config_file(tmpdir)

            self.assertEqual(config_path, Path(tmpdir).resolve() / "config.yaml")
            self.assertTrue(config_path.is_file())
            self.assertFalse((Path(tmpdir) / "my_toolkit").exists())

            config_text = config_path.read_text(encoding="utf-8")
            self.assertIn("provider:", config_text)
            self.assertIn("model:", config_text)
            self.assertNotIn("workspace:", config_text)
            self.assertNotIn("server:", config_text)
            self.assertNotIn("capabilities:", config_text)


if __name__ == "__main__":
    unittest.main()
