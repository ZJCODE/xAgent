import asyncio
import io
import tomllib
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from xagent.core.config import AgentConfig
from xagent.core.providers import (
    MODEL_API_ANTHROPIC_MESSAGES,
    MODEL_API_OPENAI_CHAT_COMPLETIONS,
    MODEL_API_OPENAI_RESPONSES,
    PROVIDER_CUSTOM,
    PROVIDER_QWEN,
    VISION_CAPABLE_PROVIDERS,
    provider_supports_vision,
    provider_model_api,
)
from xagent.interfaces.channels import enabled_channels_from_config
from xagent.interfaces.cli import (
    InitSelection,
    collect_init_selection,
    collect_init_selection_terminal_ui,
    init_agent_directory,
)
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

    def test_base_agent_prompt_requires_attachment_delivery_for_images(self):
        prompt = AgentConfig.BASE_AGENT_PROMPT

        self.assertIn("Never use Markdown image embeds", prompt)
        self.assertIn("structured attachment", prompt)
        self.assertIn("attach_artifact", prompt)

    def test_memory_defaults_are_internal_balanced_values(self):
        self.assertEqual(AgentConfig.MEMORY_RECENT_DAYS, 2)
        self.assertEqual(AgentConfig.MEMORY_STALE_FLUSH_SECONDS, 900)
        self.assertEqual(AgentConfig.MEMORY_MESSAGE_THRESHOLD, 20)
        self.assertEqual(AgentConfig.MEMORY_MIN_INTERVAL_SECONDS, 600)


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

    def test_provider_capability_detects_vision_providers(self):
        self.assertEqual(VISION_CAPABLE_PROVIDERS, frozenset({"openai", PROVIDER_QWEN}))
        self.assertTrue(provider_supports_vision({"name": "openai"}))
        self.assertTrue(provider_supports_vision({"name": PROVIDER_QWEN}))
        self.assertTrue(provider_supports_vision({"name": PROVIDER_CUSTOM, "supports_vision": True}))
        self.assertFalse(provider_supports_vision({"name": PROVIDER_CUSTOM}))
        self.assertFalse(provider_supports_vision({"name": PROVIDER_CUSTOM, "supports_vision": False}))
        self.assertFalse(provider_supports_vision({"name": "deepseek"}))
        self.assertFalse(provider_supports_vision({"name": "anthropic"}))

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

    def test_custom_provider_config_can_enable_vision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "custom"
    model_api: "openai_chat_completions"
    base_url: "https://api.example.com/v1"
    api_key: "test-key"
    model: "custom-vision-model"
    supports_vision: true
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertTrue(runner.agent.supports_vision)

    def test_custom_provider_config_rejects_non_boolean_vision_support(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "custom"
    model_api: "openai_chat_completions"
    base_url: "https://api.example.com/v1"
    api_key: "test-key"
    model: "custom-vision-model"
    supports_vision: "yes"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "provider.supports_vision must be a boolean"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_known_provider_config_accepts_manual_vision_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    base_url: "https://api.deepseek.com"
    api_key: "test-key"
    model: "deepseek-v4-pro"
    supports_vision: true
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)
            self.assertTrue(runner.agent.supports_vision)

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
            self.assertIn("manage_scheduled_tasks", runner.agent.tools)
            self.assertNotIn("schedule_task", runner.agent.tools)
            self.assertNotIn("schedule_message", runner.agent.tools)
            self.assertNotIn("schedule_command", runner.agent.tools)
            result = asyncio.run(runner.agent.tools["run_command"]("pwd"))
            self.assertEqual(result["return_code"], 0)
            self.assertEqual(Path(result["stdout"].strip()).resolve(), Path(tmpdir).resolve() / "workspace")

    def test_init_creates_config_and_identity_in_selected_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_agent_directory(tmpdir)

            self.assertTrue(result.wrote_files)
            self.assertEqual(result.config_path, Path(tmpdir).resolve() / "config.yaml")
            self.assertEqual(result.identity_path, Path(tmpdir).resolve() / "identity.md")
            self.assertEqual(result.memory_dir, Path(tmpdir).resolve() / "memory")
            self.assertEqual(result.messages_dir, Path(tmpdir).resolve() / "messages")
            self.assertEqual(result.workspace_dir, Path(tmpdir).resolve() / "workspace")
            self.assertEqual(result.skills_dir, Path(tmpdir).resolve() / "skills")
            self.assertEqual(result.tasks_dir, Path(tmpdir).resolve() / "tasks")
            self.assertTrue(result.config_path.is_file())
            self.assertTrue(result.identity_path.is_file())
            self.assertTrue(result.memory_dir.is_dir())
            self.assertTrue(result.messages_dir.is_dir())
            self.assertTrue(result.workspace_dir.is_dir())
            self.assertTrue(result.skills_dir.is_dir())
            self.assertTrue(result.tasks_dir.is_dir())
            self.assertFalse((result.memory_dir / "people").exists())
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
            self.assertEqual(config["image_generation"]["provider"], "openai")
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
            memory_marker = result.memory_dir / "daily.md"
            messages_marker = result.messages_dir / "messages.sqlite3"
            workspace_marker = result.workspace_dir / "notes.md"
            skills_marker = result.skills_dir / "code-review" / "SKILL.md"
            memory_marker.write_text("memory", encoding="utf-8")
            messages_marker.write_text("messages", encoding="utf-8")
            workspace_marker.write_text("workspace", encoding="utf-8")
            skills_marker.parent.mkdir()
            skills_marker.write_text("skills", encoding="utf-8")

            init_agent_directory(tmpdir, force=True)

            self.assertEqual(memory_marker.read_text(encoding="utf-8"), "memory")
            self.assertEqual(messages_marker.read_text(encoding="utf-8"), "messages")
            self.assertEqual(workspace_marker.read_text(encoding="utf-8"), "workspace")
            self.assertEqual(skills_marker.read_text(encoding="utf-8"), "skills")

    def test_init_force_can_clear_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_agent_directory(tmpdir)
            memory_marker = result.memory_dir / "daily.md"
            messages_marker = result.messages_dir / "messages.sqlite3"
            workspace_marker = result.workspace_dir / "notes.md"
            skills_marker = result.skills_dir / "code-review" / "SKILL.md"
            memory_marker.write_text("memory", encoding="utf-8")
            messages_marker.write_text("messages", encoding="utf-8")
            workspace_marker.write_text("workspace", encoding="utf-8")
            skills_marker.parent.mkdir()
            skills_marker.write_text("skills", encoding="utf-8")

            cleared = init_agent_directory(tmpdir, force=True, clear_runtime_data=True)

            self.assertTrue(cleared.memory_dir.is_dir())
            self.assertTrue(cleared.messages_dir.is_dir())
            self.assertTrue(cleared.workspace_dir.is_dir())
            self.assertTrue(cleared.skills_dir.is_dir())
            self.assertFalse(memory_marker.exists())
            self.assertFalse(messages_marker.exists())
            self.assertFalse(workspace_marker.exists())
            self.assertTrue(skills_marker.exists())

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
            self.assertEqual(config["image_generation"]["provider"], "none")
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

    def test_init_writes_qwen_search_for_qwen_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="qwen",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                api_key="qwen-key",
                model="qwen3-max-2026-01-23",
                identity="# Identity\n\nYou search with Qwen.\n",
                search_provider="qwen",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(config["search"], {"provider": "qwen"})

    def test_init_writes_qwen_search_key_for_non_qwen_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="deepseek",
                base_url="https://api.deepseek.com",
                api_key="deepseek-key",
                model="deepseek-v4-pro",
                identity="# Identity\n\nYou search with Qwen.\n",
                search_provider="qwen",
                search_api_key="qwen-search-key",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(config["search"]["provider"], "qwen")
            self.assertEqual(config["search"]["api_key"], "qwen-search-key")

    def test_init_writes_minimax_search_for_minimax_provider_without_duplicate_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="minimax",
                base_url="https://api.minimaxi.com/anthropic",
                api_key="minimax-key",
                model="MiniMax-M3",
                identity="# Identity\n\nYou search with MiniMax.\n",
                search_provider="minimax",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(config["search"], {"provider": "minimax"})

    def test_init_writes_minimax_search_key_for_non_minimax_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="openai",
                base_url="https://api.openai.com/v1",
                api_key="openai-key",
                model="gpt-5.4-mini",
                identity="# Identity\n\nYou search with MiniMax.\n",
                search_provider="minimax",
                search_api_key="minimax-search-key",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(config["search"]["provider"], "minimax")
            self.assertEqual(config["search"]["api_key"], "minimax-search-key")

    def test_init_rejects_openai_image_generation_for_non_openai_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="deepseek",
                base_url="https://api.deepseek.com",
                api_key="deepseek-key",
                model="deepseek-v4-pro",
                identity="# Identity\n\nYou draw with OpenAI.\n",
                image_generation_provider="openai",
                image_generation_api_key="openai-image-key",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(
                config["image_generation"],
                {"provider": "openai", "api_key": "openai-image-key"},
            )

    def test_init_writes_minimax_image_generation_key_for_non_minimax_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="deepseek",
                base_url="https://api.deepseek.com",
                api_key="deepseek-key",
                model="deepseek-v4-pro",
                identity="# Identity\n\nYou draw with MiniMax.\n",
                image_generation_provider="minimax",
                image_generation_api_key="minimax-image-key",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(
                config["image_generation"],
                {"provider": "minimax", "api_key": "minimax-image-key"},
            )

    def test_init_writes_minimax_image_generation_for_minimax_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="minimax",
                base_url="https://api.minimaxi.com/anthropic",
                api_key="minimax-key",
                model="MiniMax-M2.7",
                identity="# Identity\n\nYou draw with MiniMax.\n",
                image_generation_provider="minimax",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(config["image_generation"], {"provider": "minimax"})

    def test_init_writes_qwen_image_generation_for_qwen_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="qwen",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                api_key="qwen-key",
                model="qwen3.6-plus",
                identity="# Identity\n\nYou draw with Qwen.\n",
                image_generation_provider="qwen",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(config["image_generation"], {"provider": "qwen"})

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
            self.assertFalse(config["provider"]["supports_vision"])
            self.assertNotIn("sdk", config["provider"])
            self.assertNotIn("backend", config["provider"])

    def test_init_writes_custom_provider_vision_support(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="custom",
                model_api=MODEL_API_OPENAI_CHAT_COMPLETIONS,
                supports_vision=True,
                base_url="https://api.example.com/v1",
                api_key="secret-key",
                model="custom-model",
                identity="# Identity\n\nYou use a custom vision provider.\n",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertTrue(config["provider"]["supports_vision"])

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

    def test_init_writes_minimal_voice_config_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="openai",
                base_url="https://api.openai.com/v1",
                api_key="openai-key",
                model="gpt-5.4-mini",
                identity="# Identity\n\nYou talk.\n",
                search_provider="openai",
                voice_enabled=True,
                voice_provider="soniox",
                voice_api_key="soniox-key",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(
                config["channels"]["voice"],
                {
                    "provider": "soniox",
                    "enable_interruptions": False,
                    "audio": {"input": "auto", "output": "auto"},
                    "wake": {
                        "enabled": False,
                        "wake_phrases": ["xAgent"],
                        "exit_phrases": ["exit", "stop", "goodbye", "that's all", "never mind"],
                        "match_mode": "prefix",
                        "idle_timeout_seconds": 60,
                    },
                    "stt": {"api_key": "soniox-key", "model": "stt-rt-v4"},
                    "tts": {"api_key": "soniox-key", "model": "tts-rt-v1", "voice": "Owen"},
                },
            )

    def test_init_writes_soniox_placeholder_when_voice_key_is_blank(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="openai",
                base_url="https://api.openai.com/v1",
                api_key="openai-key",
                model="gpt-5.4-mini",
                identity="# Identity\n\nYou talk.\n",
                search_provider="openai",
                voice_enabled=True,
                voice_provider="soniox",
                voice_api_key="",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(
                config["channels"]["voice"],
                {
                    "provider": "soniox",
                    "enable_interruptions": False,
                    "audio": {"input": "auto", "output": "auto"},
                    "wake": {
                        "enabled": False,
                        "wake_phrases": ["xAgent"],
                        "exit_phrases": ["exit", "stop", "goodbye", "that's all", "never mind"],
                        "match_mode": "prefix",
                        "idle_timeout_seconds": 60,
                    },
                    "stt": {"api_key": "your_soniox_api_key_here", "model": "stt-rt-v4"},
                    "tts": {"api_key": "your_soniox_api_key_here", "model": "tts-rt-v1", "voice": "Owen"},
                },
            )

    def test_init_writes_minimal_qwen_voice_config_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="deepseek",
                base_url="https://api.deepseek.com",
                api_key="deepseek-key",
                model="deepseek-v4-pro",
                identity="# Identity\n\nYou talk.\n",
                voice_enabled=True,
                voice_provider="qwen",
                voice_api_key="qwen-voice-key",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(
                config["channels"]["voice"],
                {
                    "provider": "qwen",
                    "enable_interruptions": False,
                    "audio": {"input": "auto", "output": "auto"},
                    "wake": {
                        "enabled": False,
                        "wake_phrases": ["xAgent"],
                        "exit_phrases": ["exit", "stop", "goodbye", "that's all", "never mind"],
                        "match_mode": "prefix",
                        "idle_timeout_seconds": 60,
                    },
                    "stt": {"api_key": "qwen-voice-key", "model": "qwen3-asr-flash-realtime"},
                    "tts": {"api_key": "qwen-voice-key", "model": "qwen3-tts-flash-realtime", "voice": "Cherry"},
                },
            )

    def test_init_writes_custom_voice_config_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="openai",
                base_url="https://api.openai.com/v1",
                api_key="openai-key",
                model="gpt-5.4-mini",
                identity="# Identity\n\nYou talk.\n",
                voice_enabled=True,
                voice_provider="custom",
                voice_stt_provider="qwen",
                voice_stt_api_key="qwen-voice-key",
                voice_tts_provider="soniox",
                voice_tts_api_key="soniox-key",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertEqual(
                config["channels"]["voice"],
                {
                    "provider": "custom",
                    "enable_interruptions": False,
                    "audio": {"input": "auto", "output": "auto"},
                    "wake": {
                        "enabled": False,
                        "wake_phrases": ["xAgent"],
                        "exit_phrases": ["exit", "stop", "goodbye", "that's all", "never mind"],
                        "match_mode": "prefix",
                        "idle_timeout_seconds": 60,
                    },
                    "stt": {
                        "provider": "qwen",
                        "api_key": "qwen-voice-key",
                        "model": "qwen3-asr-flash-realtime",
                    },
                    "tts": {
                        "provider": "soniox",
                        "api_key": "soniox-key",
                        "model": "tts-rt-v1",
                        "voice": "Owen",
                    },
                },
            )

    def test_init_omits_voice_config_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selection = InitSelection(
                provider="openai",
                base_url="https://api.openai.com/v1",
                api_key="openai-key",
                model="gpt-5.4-mini",
                identity="# Identity\n\nNo voice.\n",
                search_provider="openai",
            )

            result = init_agent_directory(tmpdir, selection=selection)
            config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))

            self.assertNotIn("voice", config["channels"])

    def test_collect_init_selection_supports_custom_identity(self):
        answers = iter([
            "1",
            "4",
            "",
            "",
            "",
            "You investigate codebases.",
            ".",
        ])

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            selection = collect_init_selection(
                input_func=lambda prompt: next(answers),
                secret_input_func=lambda prompt: "openai-key",
            )

        self.assertEqual(selection.provider, "openai")
        self.assertEqual(selection.model_api, "")
        self.assertEqual(selection.base_url, "https://api.openai.com/v1")
        self.assertEqual(selection.api_key, "openai-key")
        self.assertEqual(selection.model, "gpt-5.5")
        self.assertEqual(selection.search_provider, "none")
        self.assertEqual(selection.image_generation_provider, "openai")
        self.assertEqual(selection.identity, "# Identity\n\nYou investigate codebases.\n")
        self.assertNotIn("Image generation provider", stdout.getvalue())

    def test_collect_init_selection_supports_soniox_voice(self):
        answers = iter([
            "1",
            "",
            "",
            "",
            "y",
            "2",
            ".",
        ])
        secrets = iter(["openai-key", "soniox-key"])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: next(secrets),
        )

        self.assertTrue(selection.voice_enabled)
        self.assertEqual(selection.voice_provider, "soniox")
        self.assertEqual(selection.voice_api_key, "soniox-key")

    def test_collect_init_selection_voice_blank_key_uses_placeholder(self):
        answers = iter([
            "1",
            "",
            "",
            "",
            "y",
            "2",
            ".",
        ])
        secrets = iter(["openai-key", ""])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: next(secrets),
        )

        self.assertTrue(selection.voice_enabled)
        self.assertEqual(selection.voice_provider, "soniox")
        self.assertEqual(selection.voice_api_key, "your_soniox_api_key_here")

    def test_collect_init_selection_supports_qwen_voice_key(self):
        answers = iter([
            "2",
            "1",
            "",
            "",
            "",
            "y",
            "3",
            ".",
        ])
        secrets = iter(["deepseek-key", "qwen-voice-key"])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: next(secrets),
        )

        self.assertTrue(selection.voice_enabled)
        self.assertEqual(selection.voice_provider, "qwen")
        self.assertEqual(selection.voice_api_key, "qwen-voice-key")

    def test_collect_init_selection_reuses_main_qwen_key_for_qwen_voice(self):
        answers = iter([
            "4",
            "",
            "",
            "",
            "y",
            "3",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "qwen-key",
        )

        self.assertTrue(selection.voice_enabled)
        self.assertEqual(selection.voice_provider, "qwen")
        self.assertEqual(selection.voice_api_key, "qwen-key")

    def test_collect_init_selection_supports_custom_voice_providers(self):
        events = []
        answers = iter([
            "1",
            "",
            "",
            "",
            "y",
            "4",
            "1",
            "2",
            ".",
        ])
        secrets = iter(["openai-key", "soniox-stt-key", "qwen-tts-key"])

        selection = collect_init_selection(
            input_func=lambda prompt: events.append(("input", prompt)) or next(answers),
            secret_input_func=lambda prompt: events.append(("secret", prompt)) or next(secrets),
        )

        self.assertTrue(selection.voice_enabled)
        self.assertEqual(selection.voice_provider, "custom")
        self.assertEqual(selection.voice_stt_provider, "soniox")
        self.assertEqual(selection.voice_stt_api_key, "soniox-stt-key")
        self.assertEqual(selection.voice_tts_provider, "qwen")
        self.assertEqual(selection.voice_tts_api_key, "qwen-tts-key")
        self.assertLess(
            events.index(("secret", "Soniox API key for STT (leave blank to fill in later): ")),
            events.index(("secret", "Qwen API key for TTS (leave blank to fill in later): ")),
        )

    def test_collect_init_selection_supports_langfuse_observability(self):
        answers = iter([
            "1",
            "",
            "y",
            "pk-lf-test",
            "https://jp.cloud.langfuse.com",
            "",
            "",
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
            "",
            "",
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
            "",
            "1",
            "",
            "",
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
            "",
            "y",
            "pk-lf-test",
            "",
            "",
            "",
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
            "",
            "",
            "",
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
        self.assertEqual(selection.image_generation_provider, "none")
        self.assertIn("Describe this agent's role", selection.identity)

    def test_collect_init_selection_supports_custom_model_name(self):
        answers = iter([
            "1",
            "6",
            "gpt-5.4-lab",
            "",
            "",
            "",
            ".",
        ])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: "",
        )

        self.assertEqual(selection.provider, "openai")
        self.assertEqual(selection.model, "gpt-5.4-lab")
        self.assertEqual(selection.api_key, "your_api_key_here")

    def test_collect_init_selection_terminal_ui_supports_custom_model_name(self):
        class FakeUI:
            interactive = True

            def __init__(self):
                self.model_options = []

            def select(self, *, label, subtitle="", options, default_index=0):
                del subtitle, default_index
                if label == "Provider":
                    return SimpleNamespace(key="deepseek")
                if label == "DeepSeek Model":
                    self.model_options = [option.key for option in options]
                    return SimpleNamespace(key="Custom")
                if label == "Search Provider":
                    return SimpleNamespace(key="none")
                if label == "Image Generation Provider":
                    return SimpleNamespace(key="none")
                raise AssertionError(f"Unexpected select prompt: {label}")

            def confirm(self, label, *, default=False):
                del default
                if label in {"Enable Langfuse observability?", "Enable voice mode?"}:
                    return False
                raise AssertionError(f"Unexpected confirm prompt: {label}")

            def ask_text(self, label, *, default=None, secret=False, subtitle=""):
                del default, secret, subtitle
                if label == "Custom model name":
                    return "deepseek-v4-lab"
                if label == "Identity":
                    return "Terminal identity"
                raise AssertionError(f"Unexpected text prompt: {label}")

            def ask_secret(self, prompt):
                del prompt
                return ""

        ui = FakeUI()

        selection = collect_init_selection_terminal_ui(ui=ui)

        self.assertIn("Custom", ui.model_options)
        self.assertEqual(selection.provider, "deepseek")
        self.assertEqual(selection.model, "deepseek-v4-lab")
        self.assertEqual(selection.api_key, "your_api_key_here")

    def test_collect_init_selection_supports_qwen_models(self):
        answers = iter([
            "4",
            "3",
            "",
            "",
            "",
            ".",
        ])

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            selection = collect_init_selection(
                input_func=lambda prompt: next(answers),
                secret_input_func=lambda prompt: "qwen-key",
            )

        self.assertEqual(selection.provider, "qwen")
        self.assertEqual(selection.model_api, "")
        self.assertEqual(selection.base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(selection.api_key, "qwen-key")
        self.assertEqual(selection.model, "qwen3.6-max-preview")
        self.assertEqual(selection.search_provider, "none")
        self.assertEqual(selection.image_generation_provider, "qwen")
        self.assertIn("Search provider", stdout.getvalue())
        self.assertNotIn("Image generation provider", stdout.getvalue())

    def test_collect_init_selection_supports_qwen_search_for_non_qwen_provider(self):
        answers = iter([
            "2",
            "1",
            "",
            "3",
            "",
            "",
            ".",
        ])
        secrets = iter(["deepseek-key", "qwen-search-key"])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: next(secrets),
        )

        self.assertEqual(selection.provider, "deepseek")
        self.assertEqual(selection.search_provider, "qwen")
        self.assertEqual(selection.search_api_key, "qwen-search-key")

    def test_collect_init_selection_supports_openai_search_for_non_openai_provider(self):
        answers = iter([
            "2",
            "1",
            "",
            "2",
            "",
            "",
            "",
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

    def test_collect_init_selection_supports_minimax_provider_with_builtin_anthropic_protocol(self):
        answers = iter([
            "3",
            "",
            "",
            "",
            "",
            ".",
        ])

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            selection = collect_init_selection(
                input_func=lambda prompt: next(answers),
                secret_input_func=lambda prompt: "minimax-key",
            )

        self.assertEqual(selection.model_api, "")
        self.assertEqual(selection.provider, "minimax")
        self.assertEqual(selection.base_url, "https://api.minimaxi.com/anthropic")
        self.assertEqual(selection.api_key, "minimax-key")
        self.assertEqual(selection.model, "MiniMax-M3")
        self.assertEqual(selection.search_provider, "none")
        self.assertEqual(selection.image_generation_provider, "minimax")
        self.assertNotIn("Image generation provider", stdout.getvalue())

    def test_collect_init_selection_supports_anthropic_provider(self):
        answers = iter([
            "5",
            "",
            "",
            "",
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
        self.assertEqual(selection.search_provider, "none")

    def test_collect_init_selection_custom_provider_selects_model_api_before_base_url(self):
        answers = iter([
            "6",
            "2",
            "",
            "y",
            "",
            "",
            "",
            "",
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
        self.assertTrue(selection.supports_vision)
        self.assertEqual(selection.search_provider, "none")

    def test_collect_init_selection_supports_openai_image_generation_for_non_openai_provider(self):
        answers = iter([
            "2",
            "1",
            "",
            "",
            "2",
            "",
            "",
            ".",
        ])
        secrets = iter(["deepseek-key", "openai-image-key"])

        selection = collect_init_selection(
            input_func=lambda prompt: next(answers),
            secret_input_func=lambda prompt: next(secrets),
        )

        self.assertEqual(selection.provider, "deepseek")
        self.assertEqual(selection.search_provider, "none")
        self.assertEqual(selection.image_generation_provider, "openai")
        self.assertEqual(selection.image_generation_api_key, "openai-image-key")

    def test_collect_init_selection_non_openai_includes_openai_search(self):
        prompts = []
        answers = iter([
            "2",
            "1",
            "",
            "",
            "",
            "",
            ".",
        ])

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            selection = collect_init_selection(
                input_func=lambda prompt: prompts.append(prompt) or next(answers),
                secret_input_func=lambda prompt: "deepseek-key",
            )

        search_output = stdout.getvalue().split("Search provider", 1)[1]
        self.assertEqual(selection.search_provider, "none")
        self.assertIn("openai", search_output)
        self.assertIn("qwen", search_output)
        self.assertIn("Image generation provider", stdout.getvalue())
        self.assertIn("minimax", stdout.getvalue())
        self.assertEqual(prompts[2], "Enable Langfuse observability? [y/N]: ")

    def test_collect_init_selection_does_not_label_defaults(self):
        answers = iter([
            "",
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
        self.assertEqual(selection.search_provider, "none")
        self.assertEqual(selection.image_generation_provider, "openai")
        self.assertIn("Describe this agent's role", selection.identity)
        self.assertNotIn("(default)", stdout.getvalue())
        self.assertNotIn("Image generation provider", stdout.getvalue())

    def test_config_rejects_unknown_search_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
search:
    provider: "unsupported_search"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "Unsupported search provider"):
                BaseAgentRunner(config_dir=tmpdir)

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

    def test_config_rejects_qwen_search_for_non_qwen_provider_without_search_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    model: "deepseek-v4-pro"
    api_key: "test-key"
search:
    provider: "qwen"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "requires search.api_key"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_accepts_qwen_search_for_non_qwen_provider_with_search_key(self):
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
    provider: "qwen"
    api_key: "qwen-search-key"
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
            self.assertEqual(search_provider.provider, "qwen")
            self.assertEqual(search_provider.model, "qwen3-max-2026-01-23")
            self.assertEqual(
                str(search_provider.client.base_url).rstrip("/"),
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            )

    def test_config_rejects_minimax_search_for_non_minimax_provider_without_search_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "openai"
    model: "gpt-5.4-mini"
    api_key: "test-key"
search:
    provider: "minimax"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "requires search.api_key"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_loads_minimax_search_tool_with_main_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "minimax"
    base_url: "https://api.minimaxi.com/anthropic"
    model: "MiniMax-M3"
    api_key: "minimax-key"
search:
    provider: "minimax"
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
            self.assertEqual(search_provider.provider, "minimax")
            self.assertEqual(search_provider.config["api_key"], "minimax-key")

    def test_config_loads_qwen_search_tool_with_main_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "qwen"
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: "qwen3-max-2026-01-23"
    api_key: "qwen-key"
search:
    provider: "qwen"
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
            self.assertEqual(search_provider.provider, "qwen")
            self.assertEqual(search_provider.model, "qwen3-max-2026-01-23")
            self.assertEqual(search_provider.config["api_key"], "qwen-key")
            self.assertEqual(search_provider.config["base_url"], "https://dashscope.aliyuncs.com/compatible-mode/v1")

    def test_config_loads_openai_image_generation_tool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "openai"
    model: "gpt-5.4-mini"
    api_key: "test-key"
image_generation:
    provider: "openai"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertIn("generate_image", runner.agent.tools)
            self.assertTrue(runner.agent.supports_vision)

    def test_config_loads_minimax_image_generation_tool_with_main_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "minimax"
    base_url: "https://api.minimaxi.com/anthropic"
    model: "MiniMax-M2.7"
    api_key: "minimax-key"
image_generation:
    provider: "minimax"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)
            image_tool = runner.agent.tools["generate_image"]
            image_provider = next(
                cell.cell_contents
                for cell in image_tool.__closure__
                if cell.cell_contents.__class__.__name__ == "ConfiguredImageGenerationProvider"
            )

            self.assertIn("generate_image", runner.agent.tools)
            self.assertFalse(runner.agent.supports_vision)
            self.assertEqual(image_provider.provider, "minimax")
            self.assertEqual(image_provider.config["api_key"], "minimax-key")

    def test_config_loads_qwen_image_generation_tool_with_main_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "qwen"
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: "qwen3.6-plus"
    api_key: "qwen-key"
image_generation:
    provider: "qwen"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)
            image_tool = runner.agent.tools["generate_image"]
            image_provider = next(
                cell.cell_contents
                for cell in image_tool.__closure__
                if cell.cell_contents.__class__.__name__ == "ConfiguredImageGenerationProvider"
            )

            self.assertIn("generate_image", runner.agent.tools)
            self.assertTrue(runner.agent.supports_vision)
            self.assertEqual(image_provider.provider, "qwen")
            self.assertEqual(image_provider.config["api_key"], "qwen-key")
            self.assertEqual(image_provider.config["base_url"], "https://dashscope.aliyuncs.com/compatible-mode/v1")

    def test_config_rejects_openai_image_generation_for_non_openai_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    model: "deepseek-v4-pro"
    api_key: "test-key"
image_generation:
    provider: "openai"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "requires image_generation.api_key when provider is not OpenAI"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_minimax_image_generation_for_non_minimax_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    model: "deepseek-v4-pro"
    api_key: "test-key"
image_generation:
    provider: "minimax"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "requires image_generation.api_key when provider is not MiniMax"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_qwen_image_generation_for_non_qwen_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    model: "deepseek-v4-pro"
    api_key: "test-key"
image_generation:
    provider: "qwen"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "requires image_generation.api_key when provider is not Qwen"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_accepts_openai_image_generation_for_non_openai_provider_with_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    model: "deepseek-v4-pro"
    base_url: "https://api.deepseek.com"
    api_key: "deepseek-key"
image_generation:
    provider: "openai"
    api_key: "openai-image-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)
            image_tool = runner.agent.tools["generate_image"]
            image_provider = next(
                cell.cell_contents
                for cell in image_tool.__closure__
                if cell.cell_contents.__class__.__name__ == "ConfiguredImageGenerationProvider"
            )

            self.assertEqual(image_provider.provider, "openai")
            self.assertEqual(image_provider.config["api_key"], "openai-image-key")

    def test_config_accepts_minimax_image_generation_for_non_minimax_provider_with_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    model: "deepseek-v4-pro"
    base_url: "https://api.deepseek.com"
    api_key: "deepseek-key"
image_generation:
    provider: "minimax"
    api_key: "minimax-image-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)
            image_tool = runner.agent.tools["generate_image"]
            image_provider = next(
                cell.cell_contents
                for cell in image_tool.__closure__
                if cell.cell_contents.__class__.__name__ == "ConfiguredImageGenerationProvider"
            )

            self.assertEqual(image_provider.provider, "minimax")
            self.assertEqual(image_provider.config["api_key"], "minimax-image-key")

    def test_config_accepts_qwen_image_generation_for_non_qwen_provider_with_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    name: "deepseek"
    model: "deepseek-v4-pro"
    base_url: "https://api.deepseek.com"
    api_key: "deepseek-key"
image_generation:
    provider: "qwen"
    api_key: "qwen-image-key"
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)
            image_tool = runner.agent.tools["generate_image"]
            image_provider = next(
                cell.cell_contents
                for cell in image_tool.__closure__
                if cell.cell_contents.__class__.__name__ == "ConfiguredImageGenerationProvider"
            )

            self.assertEqual(image_provider.provider, "qwen")
            self.assertEqual(image_provider.config["api_key"], "qwen-image-key")

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

    def test_config_accepts_soniox_voice_channel_without_enabling_service_all(self):
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
    voice:
        provider: soniox
        stt:
            api_key: test-soniox-key
            model: stt-rt-v4
            max_endpoint_delay_ms: 700
        tts:
            api_key: test-soniox-key
            model: tts-rt-v1
            voice: Owen
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertEqual(runner.config["channels"]["voice"]["stt"]["model"], "stt-rt-v4")
            self.assertEqual(enabled_channels_from_config(runner.config), ["api"])

    def test_voice_config_requires_provider_before_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
channels:
    voice: {}
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with patch.dict("os.environ", {"SONIOX_API_KEY": "env-soniox-key"}):
                runner = BaseAgentRunner(config_dir=tmpdir)
                with self.assertRaisesRegex(ValueError, "channels.voice.provider"):
                    runner.config["channels"]["voice"]
                    from xagent.voice.config import VoiceChannelConfig

                    VoiceChannelConfig.from_dict(runner.config["channels"]["voice"]).resolved_provider()

    def test_voice_config_rejects_top_level_api_key(self):
        from xagent.voice.config import VoiceChannelConfig

        with self.assertRaisesRegex(ValueError, "api_key"):
            VoiceChannelConfig.from_dict({"provider": "soniox", "api_key": "soniox-key"})

    def test_voice_config_rejects_missing_api_key_for_explicit_provider_even_when_env_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
channels:
    voice:
        provider: soniox
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with patch.dict("os.environ", {"SONIOX_API_KEY": "env-soniox-key"}):
                runner = BaseAgentRunner(config_dir=tmpdir)
                with self.assertRaisesRegex(ValueError, "channels.voice.stt.api_key"):
                    runner.config["channels"]["voice"]
                    from xagent.voice.config import VoiceChannelConfig

                    VoiceChannelConfig.from_dict(runner.config["channels"]["voice"]).resolved_stt_api_key()

    def test_voice_config_rejects_placeholder_api_key(self):
        from xagent.voice.config import VoiceChannelConfig

        config = VoiceChannelConfig.from_dict({"provider": "soniox", "stt": {"api_key": "your_soniox_api_key_here"}})

        with self.assertRaisesRegex(ValueError, "channels.voice.stt.api_key"):
            config.resolved_stt_api_key()

    def test_voice_config_accepts_none_provider_without_implying_soniox(self):
        from xagent.voice.config import VoiceChannelConfig

        config = VoiceChannelConfig.from_dict({"provider": "none"})

        self.assertIsNone(config.provider)
        with self.assertRaisesRegex(ValueError, "channels.voice.provider"):
            config.resolved_provider()

    def test_voice_config_rejects_qwen_placeholder_api_key(self):
        from xagent.voice.config import VoiceChannelConfig

        config = VoiceChannelConfig.from_dict({"provider": "qwen", "stt": {"api_key": "your_qwen_api_key_here"}})

        with self.assertRaisesRegex(ValueError, "channels.voice.stt.api_key"):
            config.resolved_stt_api_key()

    def test_voice_config_accepts_qwen_defaults(self):
        from xagent.voice.config import VoiceChannelConfig

        config = VoiceChannelConfig.from_dict({
            "provider": "qwen",
            "stt": {"api_key": "qwen-key"},
            "tts": {"api_key": "qwen-key"},
        })

        self.assertEqual(config.provider, "qwen")
        self.assertEqual(config.stt.provider, "qwen")
        self.assertEqual(config.stt.model, "qwen3-asr-flash-realtime")
        self.assertEqual(config.stt.audio_format, "pcm")
        self.assertEqual(config.stt.vad_threshold, 0.2)
        self.assertEqual(config.stt.silence_duration_ms, 400)
        self.assertEqual(config.tts.provider, "qwen")
        self.assertEqual(config.tts.model, "qwen3-tts-flash-realtime")
        self.assertEqual(config.tts.voice, "Cherry")
        self.assertEqual(config.tts.audio_format, "pcm")

    def test_voice_config_accepts_custom_stt_tts_providers(self):
        from xagent.voice.config import VoiceChannelConfig

        config = VoiceChannelConfig.from_dict(
            {
                "provider": "custom",
                "stt": {"provider": "qwen", "api_key": "qwen-stt-key"},
                "tts": {"provider": "soniox", "api_key": "soniox-tts-key"},
            }
        )

        self.assertEqual(config.provider, "custom")
        self.assertEqual(config.stt.provider, "qwen")
        self.assertEqual(config.stt.model, "qwen3-asr-flash-realtime")
        self.assertEqual(config.stt.audio_format, "pcm")
        self.assertEqual(config.resolved_stt_api_key(), "qwen-stt-key")
        self.assertEqual(config.tts.provider, "soniox")
        self.assertEqual(config.tts.model, "tts-rt-v1")
        self.assertEqual(config.tts.voice, "Owen")
        self.assertEqual(config.resolved_tts_api_key(), "soniox-tts-key")

    def test_config_accepts_advanced_qwen_voice_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
channels:
    voice:
        provider: qwen
        websocket_base_url: wss://dashscope.aliyuncs.com/api-ws/v1/realtime
        stt:
            api_key: qwen-key
            model: qwen3-asr-flash-realtime
            audio_format: pcm
            sample_rate: 16000
            num_channels: 1
            language: zh
            turn_detection: server_vad
            vad_threshold: 0.1
            silence_duration_ms: 500
            session_options:
                custom_stt_option: true
        tts:
            api_key: qwen-key
            model: qwen3-tts-instruct-flash-realtime
            voice: Cherry
            audio_format: pcm
            sample_rate: 24000
            language_policy: from_stt_dominant
            fallback_language: zh
            max_buffer_chars: 120
            mode: server_commit
            language_type: Auto
            instructions: "语速自然，语气友好。"
            optimize_instructions: true
            session_options:
                custom_tts_option: value
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            runner = BaseAgentRunner(config_dir=tmpdir)

            self.assertEqual(runner.config["channels"]["voice"]["provider"], "qwen")

    def test_config_rejects_unsupported_nested_voice_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
channels:
    voice:
        stt:
            provider: openai
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "voice provider must be one of"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_mixed_voice_providers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
channels:
    voice:
        provider: qwen
        stt:
            provider: soniox
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "provider must match"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_unsupported_top_level_voice_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
channels:
    voice:
        provider: openai
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "voice provider must be one of"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_invalid_voice_endpoint_delay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
channels:
    voice:
        stt:
            max_endpoint_delay_ms: 300
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "max_endpoint_delay_ms"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_config_rejects_non_pcm_voice_tts_audio_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
provider:
    model: "gpt-5.4-mini"
    api_key: "test-key"
channels:
    voice:
        tts:
            audio_format: mp3
""",
                encoding="utf-8",
            )
            write_identity(tmpdir)

            with self.assertRaisesRegex(ValueError, "voice.tts.audio_format must be one of"):
                BaseAgentRunner(config_dir=tmpdir)

    def test_voice_dependencies_are_main_project_dependencies(self):
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        dependencies = "\n".join(pyproject["project"]["dependencies"])
        self.assertNotIn("optional-dependencies", pyproject["project"])
        self.assertIn("sounddevice", dependencies)
        self.assertIn("websockets", dependencies)
        self.assertNotIn("soniox", dependencies)

    def test_readme_voice_usage_uses_single_install_path(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("pip install myxagent", readme)
        self.assertIn("xagent init", readme)
        self.assertIn("xagent voice", readme)
        self.assertNotIn("myxagent[voice]", readme)
        self.assertNotIn("SONIOX_API_KEY", readme)

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
