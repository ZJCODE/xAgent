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

    def test_turn_reply_prompt_uses_dynamic_participant_identity(self):
        prompt = AgentConfig.build_turn_reply_prompt("alice")

        self.assertIn("latest message from alice", prompt)
        self.assertIn("direct answer or action", prompt)

    def test_base_agent_prompt_describes_room_context_blocks(self):
        self.assertIn("[room context: Room]", AgentConfig.BASE_AGENT_PROMPT)
        self.assertIn("Name YYYY-MM-DD HH:mm: text", AgentConfig.BASE_AGENT_PROMPT)
        self.assertIn("[/room context]", AgentConfig.BASE_AGENT_PROMPT)


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
            self.assertEqual(result.memory_dir, Path(tmpdir).resolve() / "memory")
            self.assertEqual(result.messages_dir, Path(tmpdir).resolve() / "messages")
            self.assertTrue(result.config_path.is_file())
            self.assertTrue(result.identity_path.is_file())
            self.assertTrue(result.memory_dir.is_dir())
            self.assertTrue(result.messages_dir.is_dir())
            self.assertFalse((Path(tmpdir) / "my_toolkit").exists())

            config_text = result.config_path.read_text(encoding="utf-8")
            identity_text = result.identity_path.read_text(encoding="utf-8")
            config = yaml.safe_load(config_text)
            self.assertNotIn("agent", config)
            self.assertEqual(config["provider"]["base_url"], "https://api.openai.com/v1")
            self.assertEqual(config["provider"]["api_key"], "your_api_key_here")
            self.assertEqual(config["provider"]["model"], "gpt-5.4-mini")
            self.assertEqual(config["provider"]["name"], "openai")
            self.assertEqual(config["search"]["provider"], "openai")
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

    def test_init_force_keeps_runtime_data_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_agent_directory(tmpdir)
            memory_marker = result.memory_dir / "daily.md"
            messages_marker = result.messages_dir / "messages.sqlite3"
            memory_marker.write_text("memory", encoding="utf-8")
            messages_marker.write_text("messages", encoding="utf-8")

            init_agent_directory(tmpdir, force=True)

            self.assertEqual(memory_marker.read_text(encoding="utf-8"), "memory")
            self.assertEqual(messages_marker.read_text(encoding="utf-8"), "messages")

    def test_init_force_can_clear_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_agent_directory(tmpdir)
            memory_marker = result.memory_dir / "daily.md"
            messages_marker = result.messages_dir / "messages.sqlite3"
            memory_marker.write_text("memory", encoding="utf-8")
            messages_marker.write_text("messages", encoding="utf-8")

            cleared = init_agent_directory(tmpdir, force=True, clear_runtime_data=True)

            self.assertTrue(cleared.memory_dir.is_dir())
            self.assertTrue(cleared.messages_dir.is_dir())
            self.assertFalse(memory_marker.exists())
            self.assertFalse(messages_marker.exists())

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
            self.assertEqual(config["provider"]["name"], "deepseek")
            self.assertEqual(config["search"]["provider"], "none")
            self.assertEqual(result.identity_path.read_text(encoding="utf-8"), "# Identity\n\nYou report weather.\n")

    def test_collect_init_selection_supports_custom_identity(self):
        answers = iter([
            "1",
            "4",
            "1",
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
        self.assertEqual(selection.search_provider, "openai")
        self.assertEqual(selection.identity, "# Identity\n\nYou investigate codebases.\n")

    def test_collect_init_selection_deepseek_decide_later_uses_model_placeholder(self):
        answers = iter([
            "2",
            "3",
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
        self.assertEqual(selection.search_provider, "none")
        self.assertIn("Describe this agent's role", selection.identity)

    def test_collect_init_selection_supports_qwen_models(self):
        answers = iter([
            "3",
            "3",
            "1",
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
        self.assertEqual(selection.search_provider, "duckduckgo")

    def test_collect_init_selection_supports_brave_search_api_key(self):
        answers = iter([
            "1",
            "",
            "3",
            ".",
        ])
        secrets = iter(["openai-key", "brave-key"])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: next(secrets),
        )

        self.assertEqual(selection.provider, "openai")
        self.assertEqual(selection.search_provider, "brave")
        self.assertEqual(selection.search_api_key, "brave-key")

    def test_collect_init_selection_non_openai_excludes_openai_search(self):
        answers = iter([
            "2",
            "1",
            "1",
            ".",
        ])

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            selection = collect_init_selection(
                input_func=lambda prompt: next(answers),
                secret_input_func=lambda prompt: "deepseek-key",
            )

        search_output = stdout.getvalue().split("Search provider", 1)[1].split("Enter the agent identity", 1)[0]
        self.assertEqual(selection.search_provider, "duckduckgo")
        self.assertNotIn("openai", search_output)

    def test_collect_init_selection_does_not_label_defaults(self):
        answers = iter([
            "",
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
        self.assertEqual(selection.search_provider, "openai")
        self.assertIn("Describe this agent's role", selection.identity)
        self.assertNotIn("(default)", stdout.getvalue())

    def test_config_loads_duckduckgo_search_tool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
search:
    provider: "duckduckgo"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertIn("run_command", runner.agent.tools)
            self.assertIn("web_search", runner.agent.tools)

    def test_config_skips_search_tool_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
search:
    provider: "none"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertIn("run_command", runner.agent.tools)
            self.assertNotIn("web_search", runner.agent.tools)

    def test_config_rejects_openai_search_for_non_openai_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    model: "deepseek-v4-pro"
    api_key: "test-key"
search:
    provider: "openai"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "search.provider 'openai'"):
                BaseAgentRunner(config_dir=tmpdir)

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
