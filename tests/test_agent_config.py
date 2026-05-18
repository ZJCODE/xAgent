import io
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

from xagent.core.config import AgentConfig
from xagent.core.providers import (
    MODEL_API_ANTHROPIC_MESSAGES,
    MODEL_API_OPENAI_CHAT_COMPLETIONS,
    MODEL_API_OPENAI_RESPONSES,
    provider_model_api,
)
from xagent.interfaces.channels import enabled_channels_from_config
from xagent.interfaces.cli import InitSelection, collect_init_selection, init_agent_directory
from xagent.interfaces.base import BaseAgentRunner


def write_identity(directory: str, text: str = "You are a test assistant.") -> None:
    (Path(directory) / "identity.md").write_text(text, encoding="utf-8")


class RunnerWithoutAgent(BaseAgentRunner):
    def _initialize_agent(self):
        return object()


class AgentConfigPromptTests(unittest.TestCase):

    def test_turn_reply_prompt_uses_dynamic_participant_identity(self):
        prompt = AgentConfig.build_turn_reply_prompt("alice")

        self.assertIn("what alice most recently said", prompt)
        self.assertIn("Reply to the current message", prompt)
        self.assertIn("outcome alice needs now", prompt)

    def test_base_agent_prompt_describes_room_context_blocks(self):
        self.assertIn("[room context]", AgentConfig.BASE_AGENT_PROMPT)
        self.assertIn("room_name: ...", AgentConfig.BASE_AGENT_PROMPT)
        self.assertIn("room_id: ...", AgentConfig.BASE_AGENT_PROMPT)
        self.assertIn("Name YYYY-MM-DD HH:mm: text", AgentConfig.BASE_AGENT_PROMPT)
        self.assertIn("[/room context]", AgentConfig.BASE_AGENT_PROMPT)

    def test_memory_defaults_are_internal_balanced_values(self):
        self.assertEqual(AgentConfig.MEMORY_RECENT_DAYS, 3)
        self.assertEqual(AgentConfig.MEMORY_STALE_FLUSH_SECONDS, 180)
        self.assertEqual(AgentConfig.MEMORY_MESSAGE_THRESHOLD, 12)
        self.assertEqual(AgentConfig.MEMORY_MIN_INTERVAL_SECONDS, 300)


class ProviderConfigTests(unittest.TestCase):
    def test_provider_config_determines_model_api_protocol(self):
        self.assertEqual(
            provider_model_api({"name": "openai"}),
            MODEL_API_OPENAI_RESPONSES,
        )
        self.assertEqual(
            provider_model_api({"name": "deepseek"}),
            MODEL_API_OPENAI_CHAT_COMPLETIONS,
        )
        self.assertEqual(
            provider_model_api({"name": "qwen"}),
            MODEL_API_OPENAI_CHAT_COMPLETIONS,
        )
        self.assertEqual(
            provider_model_api({"name": "custom", "model_api": MODEL_API_OPENAI_CHAT_COMPLETIONS}),
            MODEL_API_OPENAI_CHAT_COMPLETIONS,
        )
        self.assertEqual(
            provider_model_api({"name": "custom", "sdk": "openai"}),
            MODEL_API_OPENAI_CHAT_COMPLETIONS,
        )
        self.assertEqual(
            provider_model_api({"name": "anthropic"}),
            MODEL_API_ANTHROPIC_MESSAGES,
        )
        self.assertEqual(
            provider_model_api({"name": "minimax"}),
            MODEL_API_ANTHROPIC_MESSAGES,
        )

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
            self.assertEqual(runner.agent.model_api, MODEL_API_OPENAI_RESPONSES)
            self.assertEqual(str(runner.agent.client.base_url).rstrip("/"), "https://api.openai.com/v1")

    def test_provider_config_builds_anthropic_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "minimax"
    model: "MiniMax-M2.7"
    base_url: "https://api.minimaxi.com/anthropic"
    api_key: "test-key"
search:
    provider: "none"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertEqual(runner.agent.model, "MiniMax-M2.7")
            self.assertNotIn("backend", runner.config["provider"])
            self.assertEqual(runner.agent.model_api, MODEL_API_ANTHROPIC_MESSAGES)
            self.assertEqual(runner.agent.model_client.model_api, MODEL_API_ANTHROPIC_MESSAGES)
            self.assertEqual(str(runner.agent.client.base_url).rstrip("/"), "https://api.minimaxi.com/anthropic")

    def test_provider_config_rejects_backend_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "openai"
    backend: "openai"
    model: "gpt-5.4-mini"
    api_key: "test-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, r"Unsupported provider key\(s\): backend"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_provider_config_requires_model_api_for_custom_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "custom"
    model: "custom-model"
    base_url: "https://api.example.com/v1"
    api_key: "test-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "provider.model_api is required"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_provider_config_uses_custom_anthropic_model_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "custom"
    model_api: "anthropic_messages"
    model: "custom-model"
    base_url: "https://api.example.com/anthropic"
    api_key: "test-key"
search:
    provider: "none"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertEqual(runner.agent.model_api, MODEL_API_ANTHROPIC_MESSAGES)
            self.assertEqual(str(runner.agent.client.base_url).rstrip("/"), "https://api.example.com/anthropic")

    def test_deepseek_runner_uses_chat_completions_protocol(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    model: "deepseek-v4-pro"
    base_url: "https://api.deepseek.com"
    api_key: "test-key"
search:
    provider: "none"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertEqual(runner.agent.model_api, MODEL_API_OPENAI_CHAT_COMPLETIONS)
            self.assertEqual(runner.agent.model_client.model_api, MODEL_API_OPENAI_CHAT_COMPLETIONS)

    def test_custom_openai_runner_uses_chat_completions_protocol(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "custom"
    model_api: "openai_chat_completions"
    model: "custom-model"
    base_url: "https://api.example.com/v1"
    api_key: "test-key"
search:
    provider: "none"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertEqual(runner.agent.model_api, MODEL_API_OPENAI_CHAT_COMPLETIONS)
            self.assertEqual(runner.agent.model_client.model_api, MODEL_API_OPENAI_CHAT_COMPLETIONS)

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
            self.assertNotIn("backend", config["provider"])
            self.assertNotIn("model_api", config["provider"])
            self.assertNotIn("sdk", config["provider"])
            self.assertEqual(config["search"]["provider"], "openai")
            self.assertNotIn("enabled", config["channels"]["api"])
            self.assertNotIn("web_ui", config["channels"]["api"])
            self.assertEqual(config["channels"]["api"]["host"], "127.0.0.1")
            self.assertEqual(config["channels"]["api"]["port"], 8010)
            self.assertEqual(enabled_channels_from_config(config), ["api"])
            self.assertNotIn("runtime", config)
            self.assertNotIn("memory", config)
            self.assertNotIn("observability", config)
            self.assertNotIn("system_prompt:", config_text)
            self.assertNotIn("output_schema:", config_text)
            self.assertNotIn("runtime:", config_text)
            self.assertNotIn("memory:", config_text)
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
            memory_marker = result.memory_dir / "memory.sqlite3"
            messages_marker = result.messages_dir / "messages.sqlite3"
            memory_marker.write_text("memory", encoding="utf-8")
            messages_marker.write_text("messages", encoding="utf-8")

            init_agent_directory(tmpdir, force=True)

            self.assertEqual(memory_marker.read_text(encoding="utf-8"), "memory")
            self.assertEqual(messages_marker.read_text(encoding="utf-8"), "messages")

    def test_init_force_can_clear_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_agent_directory(tmpdir)
            memory_marker = result.memory_dir / "memory.sqlite3"
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
            self.assertNotIn("backend", config["provider"])
            self.assertNotIn("model_api", config["provider"])
            self.assertNotIn("sdk", config["provider"])
            self.assertEqual(config["search"]["provider"], "none")
            self.assertEqual(result.identity_path.read_text(encoding="utf-8"), "# Identity\n\nYou report weather.\n")

    def test_init_writes_openai_search_key_for_non_openai_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="deepseek",
                base_url="https://api.deepseek.com",
                api_key="deepseek-key",
                model="deepseek-v4-pro",
                identity="# Identity\n\nYou search with OpenAI.\n",
                search_provider="openai",
                search_api_key="openai-search-key",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(config["search"]["provider"], "openai")
            self.assertEqual(config["search"]["api_key"], "openai-search-key")

    def test_init_writes_model_api_only_for_custom_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="custom",
                model_api=MODEL_API_ANTHROPIC_MESSAGES,
                base_url="https://api.example.com/anthropic",
                api_key="secret-key",
                model="custom-model",
                identity="# Identity\n\nYou use a custom provider.\n",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(config["provider"]["name"], "custom")
            self.assertEqual(config["provider"]["model_api"], MODEL_API_ANTHROPIC_MESSAGES)
            self.assertNotIn("sdk", config["provider"])
            self.assertNotIn("backend", config["provider"])

    def test_init_writes_observability_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="openai",
                base_url="https://api.openai.com/v1",
                api_key="secret-key",
                model="gpt-5.4-mini",
                identity="# Identity\n\nYou report traces.\n",
                search_provider="none",
                observability_enabled=True,
                langfuse_public_key="pk-lf-test",
                langfuse_secret_key="sk-lf-test",
                langfuse_base_url="https://us.cloud.langfuse.com",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(
                config["observability"],
                {
                    "enabled": True,
                    "provider": "langfuse",
                    "public_key": "pk-lf-test",
                    "secret_key": "sk-lf-test",
                    "base_url": "https://us.cloud.langfuse.com",
                },
            )

    def test_collect_init_selection_supports_custom_identity(self):
        answers = iter([
            "1",
            "4",
            "1",
            "",
            "You investigate codebases.",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "openai-key",
        )

        self.assertEqual(selection.provider, "openai")
        self.assertEqual(selection.model_api, "")
        self.assertEqual(selection.base_url, "https://api.openai.com/v1")
        self.assertEqual(selection.api_key, "openai-key")
        self.assertEqual(selection.model, "gpt-5.5")
        self.assertEqual(selection.search_provider, "openai")
        self.assertEqual(selection.identity, "# Identity\n\nYou investigate codebases.\n")

    def test_collect_init_selection_supports_langfuse_observability(self):
        answers = iter([
            "1",
            "",
            "4",
            "y",
            "pk-lf-test",
            "https://jp.cloud.langfuse.com",
            ".",
        ])
        secrets = iter(["openai-key", "sk-lf-test"])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: next(secrets),
        )

        self.assertTrue(selection.observability_enabled)
        self.assertEqual(selection.langfuse_public_key, "pk-lf-test")
        self.assertEqual(selection.langfuse_secret_key, "sk-lf-test")
        self.assertEqual(selection.langfuse_base_url, "https://jp.cloud.langfuse.com")

    def test_collect_init_selection_skips_langfuse_prompt_for_anthropic_protocol(self):
        answers = iter([
            "5",
            "",
            "1",
            ".",
        ])

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            selection = collect_init_selection(
                input_func=lambda prompt: next(answers),
                secret_input_func=lambda prompt: "anthropic-key",
            )

        self.assertFalse(selection.observability_enabled)
        self.assertNotIn("Enable Langfuse observability?", stdout.getvalue())

    def test_collect_init_selection_skips_langfuse_prompt_for_custom_anthropic_protocol(self):
        answers = iter([
            "6",
            "3",
            "",
            "1",
            ".",
        ])

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            selection = collect_init_selection(
                input_func=lambda prompt: next(answers),
                secret_input_func=lambda prompt: "custom-key",
            )

        self.assertEqual(selection.model_api, MODEL_API_ANTHROPIC_MESSAGES)
        self.assertFalse(selection.observability_enabled)
        self.assertNotIn("Enable Langfuse observability?", stdout.getvalue())

    def test_collect_init_selection_prompts_langfuse_for_custom_openai_protocol(self):
        answers = iter([
            "6",
            "1",
            "",
            "1",
            "y",
            "pk-lf-test",
            "",
            ".",
        ])
        secrets = iter(["custom-key", "sk-lf-test"])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: next(secrets),
        )

        self.assertEqual(selection.model_api, MODEL_API_OPENAI_CHAT_COMPLETIONS)
        self.assertTrue(selection.observability_enabled)
        self.assertEqual(selection.langfuse_public_key, "pk-lf-test")
        self.assertEqual(selection.langfuse_secret_key, "sk-lf-test")
        self.assertEqual(selection.langfuse_base_url, "https://cloud.langfuse.com")

    def test_collect_init_selection_deepseek_decide_later_uses_model_placeholder(self):
        answers = iter([
            "2",
            "3",
            "4",
            "",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "",
        )

        self.assertEqual(selection.provider, "deepseek")
        self.assertEqual(selection.model_api, "")
        self.assertEqual(selection.base_url, "https://api.deepseek.com")
        self.assertEqual(selection.api_key, "your_api_key_here")
        self.assertEqual(selection.model, "your_model_here")
        self.assertEqual(selection.search_provider, "none")
        self.assertIn("Describe this agent's role", selection.identity)

    def test_collect_init_selection_supports_qwen_models(self):
        answers = iter([
            "4",
            "3",
            "1",
            "",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "qwen-key",
        )

        self.assertEqual(selection.provider, "qwen")
        self.assertEqual(selection.model_api, "")
        self.assertEqual(selection.base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(selection.api_key, "qwen-key")
        self.assertEqual(selection.model, "qwen3.6-max-preview")
        self.assertEqual(selection.search_provider, "duckduckgo")

    def test_collect_init_selection_supports_openai_search_for_non_openai_provider(self):
        answers = iter([
            "2",
            "1",
            "2",
            "",
            ".",
        ])
        secrets = iter(["deepseek-key", "openai-search-key"])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: next(secrets),
        )

        self.assertEqual(selection.provider, "deepseek")
        self.assertEqual(selection.search_provider, "openai")
        self.assertEqual(selection.search_api_key, "openai-search-key")

    def test_collect_init_selection_supports_brave_search_api_key(self):
        answers = iter([
            "1",
            "",
            "3",
            "",
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

    def test_collect_init_selection_supports_minimax_provider_with_builtin_anthropic_protocol(self):
        answers = iter([
            "3",
            "",
            "1",
            "",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "minimax-key",
        )

        self.assertEqual(selection.model_api, "")
        self.assertEqual(selection.provider, "minimax")
        self.assertEqual(selection.base_url, "https://api.minimaxi.com/anthropic")
        self.assertEqual(selection.api_key, "minimax-key")
        self.assertEqual(selection.model, "MiniMax-M2.7")
        self.assertEqual(selection.search_provider, "duckduckgo")

    def test_collect_init_selection_supports_anthropic_provider(self):
        answers = iter([
            "5",
            "",
            "1",
            "",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "anthropic-key",
        )

        self.assertEqual(selection.model_api, "")
        self.assertEqual(selection.provider, "anthropic")
        self.assertEqual(selection.base_url, "https://api.anthropic.com")
        self.assertEqual(selection.api_key, "anthropic-key")
        self.assertEqual(selection.model, "claude-sonnet-4-20250514")
        self.assertEqual(selection.search_provider, "duckduckgo")

    def test_collect_init_selection_custom_provider_selects_model_api_before_base_url(self):
        answers = iter([
            "6",
            "2",
            "",
            "1",
            "",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "custom-key",
        )

        self.assertEqual(selection.provider, "custom")
        self.assertEqual(selection.model_api, MODEL_API_OPENAI_RESPONSES)
        self.assertEqual(selection.base_url, "https://api.example.com/v1")
        self.assertEqual(selection.api_key, "custom-key")
        self.assertEqual(selection.model, "your_model_here")
        self.assertEqual(selection.search_provider, "duckduckgo")

    def test_collect_init_selection_non_openai_includes_openai_search(self):
        answers = iter([
            "2",
            "1",
            "1",
            "",
            ".",
        ])

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            selection = collect_init_selection(
                input_func=lambda prompt: next(answers),
                secret_input_func=lambda prompt: "deepseek-key",
            )

        search_output = stdout.getvalue().split("Search provider", 1)[1].split("Enter the agent identity", 1)[0]
        self.assertEqual(selection.search_provider, "duckduckgo")
        self.assertIn("openai", search_output)

    def test_collect_init_selection_does_not_label_defaults(self):
        answers = iter([
            "",
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

    def test_config_rejects_openai_search_for_non_openai_provider_without_search_key(self):
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

            with self.assertRaisesRegex(ValueError, "requires search.api_key"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_accepts_openai_search_for_non_openai_provider_with_search_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    model: "deepseek-v4-pro"
    base_url: "https://api.deepseek.com"
    api_key: "deepseek-key"
search:
    provider: "openai"
    api_key: "openai-search-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)
            web_search_tool = runner.agent.tools["web_search"]
            search_provider = next(
                cell.cell_contents
                for cell in web_search_tool.__closure__
                if cell.cell_contents.__class__.__name__ == "ConfiguredSearchProvider"
            )

            self.assertIn("web_search", runner.agent.tools)
            self.assertEqual(search_provider.model, AgentConfig.DEFAULT_MODEL)
            self.assertEqual(
                str(search_provider.client.base_url).rstrip("/"),
                "https://api.openai.com/v1",
            )

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

    def test_config_accepts_channels_and_runtime_sections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
channels:
    api:
        host: 127.0.0.1
        port: 8010
runtime:
    heartbeat_enabled: false
    heartbeat_interval_seconds: 12
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertFalse(runner.config["runtime"]["heartbeat_enabled"])
            self.assertEqual(runner.config["runtime"]["heartbeat_interval_seconds"], 12)
            self.assertNotIn("enabled", runner.config["channels"]["api"])
            self.assertEqual(enabled_channels_from_config(runner.config), ["api"])

    def test_config_rejects_memory_section_as_unsupported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
memory:
    recent_days: 9
    stale_flush_seconds: 30
    message_threshold: 3
    min_interval_seconds: 0
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, r"Unsupported config key\(s\): memory"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_accepts_disabled_observability_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
observability:
    enabled: false
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertFalse(runner.observability.enabled)

    def test_config_accepts_enabled_langfuse_observability(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
observability:
    enabled: true
    provider: langfuse
    public_key: pk-lf-test
    secret_key: sk-lf-test
    base_url: https://cloud.langfuse.com
    sample_rate: 0.5
    debug: false
    tracing_enabled: true
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = RunnerWithoutAgent(config_dir=tmpdir)

            self.assertTrue(runner.observability.enabled)

    def test_config_rejects_invalid_observability_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
observability:
    enabled: true
    provider: other
    public_key: pk-lf-test
    secret_key: sk-lf-test
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "observability.provider"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_enabled_observability_without_secret_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
observability:
    enabled: true
    provider: langfuse
    public_key: pk-lf-test
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "observability.secret_key"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_invalid_observability_sample_rate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
observability:
    enabled: true
    provider: langfuse
    public_key: pk-lf-test
    secret_key: sk-lf-test
    sample_rate: 2
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "observability.sample_rate"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_invalid_runtime_heartbeat_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
runtime:
    heartbeat_enabled: "yes"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "runtime.heartbeat_enabled"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_invalid_runtime_heartbeat_interval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
runtime:
    heartbeat_interval_seconds: 0
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "runtime.heartbeat_interval_seconds"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_unsupported_channel_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
channels:
    custom:
        enabled: true
runtime:
    default_channel: api
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, r"Unsupported channels key\(s\): custom"):
                BaseAgentRunner(config_dir=tmpdir)


if __name__ == "__main__":
    unittest.main()
