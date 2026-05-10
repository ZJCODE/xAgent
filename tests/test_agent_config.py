import io
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

from xagent.core.config import AgentConfig
from xagent.interfaces.cli import InitSelection, collect_init_selection, init_agent_directory
from xagent.interfaces.base import BaseAgentRunner


def write_identity(directory: str, text: str = "You are a test assistant.") -> None:
    (Path(directory) / "identity.md").write_text(text, encoding="utf-8")


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
provider:
    model: "deepseek-v4-pro"
    base_url: "https://api.openai.com/v1"
    api_key: "test-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertEqual(runner.agent.system_prompt, "You are a test assistant.")
            self.assertEqual(runner.agent.model, "deepseek-v4-pro")
            self.assertEqual(str(runner.agent.client.base_url).rstrip("/"), "https://api.openai.com/v1")

    def test_default_run_command_is_not_configured_in_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertIn("run_command", runner.agent.tools)

    def test_init_creates_config_and_identity_in_selected_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_agent_directory(tmpdir)

            self.assertTrue(result.wrote_files)
            self.assertEqual(result.config_path, Path(tmpdir).resolve() / "config.yaml")
            self.assertEqual(result.identity_path, Path(tmpdir).resolve() / "identity.md")
            self.assertTrue(result.config_path.is_file())
            self.assertTrue(result.identity_path.is_file())
            self.assertFalse((Path(tmpdir) / "my_toolkit").exists())

            config_text = result.config_path.read_text(encoding="utf-8")
            identity_text = result.identity_path.read_text(encoding="utf-8")
            config = yaml.safe_load(config_text)
            self.assertNotIn("agent", config)
            self.assertEqual(config["provider"]["base_url"], "https://api.openai.com/v1")
            self.assertEqual(config["provider"]["api_key"], "your_api_key_here")
            self.assertEqual(config["provider"]["model"], "gpt-5.4-mini")
            self.assertNotIn("system_prompt:", config_text)
            self.assertNotIn("output_schema:", config_text)
            self.assertNotIn("workspace:", config_text)
            self.assertNotIn("server:", config_text)
            self.assertNotIn("capabilities:", config_text)
            self.assertIn("You are a helpful assistant.", identity_text)

    def test_init_schema_option_adds_output_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_agent_directory(tmpdir, schema=True)

            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            output_schema = config["output_schema"]
            self.assertEqual(output_schema["class_name"], "WeatherReport")
            self.assertIn("temperature_celsius", output_schema["fields"])

    def test_init_refuses_to_overwrite_managed_files_without_force(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_agent_directory(tmpdir)
            result.config_path.write_text("custom config", encoding="utf-8")

            refused = init_agent_directory(tmpdir)

            self.assertFalse(refused.wrote_files)
            self.assertIn(result.config_path, refused.conflicts)
            self.assertEqual(result.config_path.read_text(encoding="utf-8"), "custom config")

    def test_init_force_overwrites_managed_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_agent_directory(tmpdir)
            result.config_path.write_text("custom config", encoding="utf-8")
            result.identity_path.write_text("custom identity", encoding="utf-8")

            forced = init_agent_directory(tmpdir, force=True)

            self.assertTrue(forced.wrote_files)
            config = yaml.safe_load(forced.config_path.read_text(encoding="utf-8"))
            self.assertNotIn("agent", config)
            self.assertIn("You are a helpful assistant.", forced.identity_path.read_text(encoding="utf-8"))

    def test_init_uses_selected_provider_model_key_and_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="deepseek",
                base_url="https://api.deepseek.com",
                api_key="secret-key",
                model="deepseek-v4-pro",
                identity="# Identity\n\nYou report weather.\n",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertNotIn("agent", config)
            self.assertEqual(config["provider"]["base_url"], "https://api.deepseek.com")
            self.assertEqual(config["provider"]["api_key"], "secret-key")
            self.assertEqual(config["provider"]["model"], "deepseek-v4-pro")
            self.assertEqual(result.identity_path.read_text(encoding="utf-8"), "# Identity\n\nYou report weather.\n")

    def test_collect_init_selection_supports_custom_identity(self):
        answers = iter([
            "1",
            "4",
            "You investigate codebases.",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "openai-key",
        )

        self.assertEqual(selection.provider, "openai")
        self.assertEqual(selection.base_url, "https://api.openai.com/v1")
        self.assertEqual(selection.api_key, "openai-key")
        self.assertEqual(selection.model, "gpt-5.5")
        self.assertEqual(selection.identity, "# Identity\n\nYou investigate codebases.\n")

    def test_collect_init_selection_deepseek_decide_later_uses_model_placeholder(self):
        answers = iter([
            "2",
            "3",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "",
        )

        self.assertEqual(selection.provider, "deepseek")
        self.assertEqual(selection.base_url, "https://api.deepseek.com")
        self.assertEqual(selection.api_key, "your_api_key_here")
        self.assertEqual(selection.model, "your_model_here")
        self.assertIn("Describe this agent's role", selection.identity)

    def test_collect_init_selection_supports_qwen_models(self):
        answers = iter([
            "3",
            "3",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "qwen-key",
        )

        self.assertEqual(selection.provider, "qwen")
        self.assertEqual(selection.base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(selection.api_key, "qwen-key")
        self.assertEqual(selection.model, "qwen3.6-max-preview")

    def test_collect_init_selection_does_not_label_defaults(self):
        answers = iter([
            "",
            "",
            ".",
        ])

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            selection = collect_init_selection(
                input_func=lambda prompt: next(answers),
                secret_input_func=lambda prompt: "",
            )

        self.assertEqual(selection.provider, "openai")
        self.assertEqual(selection.model, "gpt-5.4-mini")
        self.assertIn("Describe this agent's role", selection.identity)
        self.assertNotIn("(default)", stdout.getvalue())

    def test_config_rejects_name_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
name: "PromptAgent"
provider:
  model: "gpt-5.4-mini"
  api_key: "test-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "Unsupported config key"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_agent_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
agent:
  provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "Unsupported config key"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_system_prompt_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
system_prompt: "not supported"
provider:
  model: "gpt-5.4-mini"
  api_key: "test-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "Unsupported config key"):
                BaseAgentRunner(config_dir=tmpdir)


if __name__ == "__main__":
    unittest.main()
