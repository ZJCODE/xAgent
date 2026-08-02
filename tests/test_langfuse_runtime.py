"""Unit tests for Langfuse observability runtime helpers."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from xagent.integrations.langfuse.runtime import (
    LangfuseObservabilityRuntime,
    NoopObservabilityRuntime,
    build_session_id,
    create_observability_runtime,
)


class BuildSessionIdTests(unittest.TestCase):
    def test_uses_channel_and_user(self):
        self.assertEqual(
            build_session_id(channel="feishu", user_id="u1"),
            "feishu:u1",
        )

    def test_prefers_room_name_over_user(self):
        self.assertEqual(
            build_session_id(channel="api", room_name="room-a", user_id="u1"),
            "api:room-a",
        )

    def test_truncates_long_session_ids(self):
        session_id = build_session_id(channel="c", user_id="x" * 250)
        self.assertEqual(len(session_id), 200)
        self.assertTrue(session_id.startswith("c:"))


class CreateObservabilityRuntimeTests(unittest.TestCase):
    def test_disabled_returns_noop(self):
        runtime = create_observability_runtime({"enabled": False, "provider": "langfuse"})
        self.assertIsInstance(runtime, NoopObservabilityRuntime)
        self.assertFalse(runtime.enabled)

    def test_non_langfuse_provider_returns_noop(self):
        runtime = create_observability_runtime(
            {
                "enabled": True,
                "provider": "other",
                "public_key": "pk",
                "secret_key": "sk",
            }
        )
        self.assertIsInstance(runtime, NoopObservabilityRuntime)

    def test_enabled_langfuse_returns_runtime(self):
        runtime = create_observability_runtime(
            {
                "enabled": True,
                "provider": "langfuse",
                "public_key": "pk-lf-test",
                "secret_key": "sk-lf-test",
                "base_url": "https://cloud.langfuse.com",
            }
        )
        self.assertIsInstance(runtime, LangfuseObservabilityRuntime)
        self.assertTrue(runtime.enabled)


class NoopObservabilityRuntimeTests(unittest.TestCase):
    def test_agent_turn_and_tool_call_are_noop(self):
        runtime = NoopObservabilityRuntime()
        with runtime.agent_turn(
            user_id="u",
            session_id="local:u",
            model="m",
            channel="local",
            stream=False,
        ) as turn:
            turn.set_input([{"role": "user", "content": "hi"}])
            turn.set_output("ok")
        with runtime.tool_call(name="lookup", call_id="c1", arguments={"q": "x"}) as tool:
            tool.set_output("done")
            tool.set_error("nope")


class LangfuseObservabilityRuntimeTests(unittest.TestCase):
    def test_agent_turn_uses_agent_type_and_propagates_session(self):
        runtime = LangfuseObservabilityRuntime(
            {
                "public_key": "pk-lf-test",
                "secret_key": "sk-lf-test",
                "base_url": "https://cloud.langfuse.com",
                "environment": "staging",
                "release": "0.3.21",
            }
        )
        span = MagicMock()
        client = MagicMock()

        @contextmanager
        def _observation(**kwargs):
            self.assertEqual(kwargs["as_type"], "agent")
            self.assertEqual(kwargs["name"], "xagent.chat")
            yield span

        client.start_as_current_observation = _observation
        propagate = MagicMock()

        @contextmanager
        def _propagate(**kwargs):
            self.assertEqual(kwargs["user_id"], "alice")
            self.assertEqual(kwargs["session_id"], "api:alice")
            self.assertIn("xagent", kwargs["tags"])
            self.assertEqual(kwargs["metadata"]["channel"], "api")
            yield None

        propagate.side_effect = lambda **kwargs: _propagate(**kwargs)

        with patch.object(runtime, "_ensure_langfuse_client", return_value=client), patch(
            "langfuse.propagate_attributes", propagate
        ):
            with runtime.agent_turn(
                user_id="alice",
                session_id="api:alice",
                model="gpt-test",
                channel="api",
                stream=True,
            ) as turn:
                turn.set_input(
                    [
                        {"role": "system", "content": "sys"},
                        {"role": "user", "content": "hello world"},
                    ]
                )
                turn.set_output("reply")

        span.update.assert_any_call(
            input={
                "total": 2,
                "roles": {"system": 1, "user": 1},
                "user": "hello world",
            }
        )
        span.update.assert_any_call(output={"content": "reply"})

    def test_tool_call_uses_tool_observation_type(self):
        runtime = LangfuseObservabilityRuntime(
            {
                "public_key": "pk-lf-test",
                "secret_key": "sk-lf-test",
            }
        )
        span = MagicMock()
        client = MagicMock()

        @contextmanager
        def _observation(**kwargs):
            self.assertEqual(kwargs["as_type"], "tool")
            self.assertEqual(kwargs["name"], "lookup")
            self.assertEqual(kwargs["input"], {"preview": '{"q": "weather"}'})
            self.assertEqual(kwargs["metadata"]["call_id"], "call-1")
            yield span

        client.start_as_current_observation = _observation

        with patch.object(runtime, "_ensure_langfuse_client", return_value=client):
            with runtime.tool_call(
                name="lookup",
                call_id="call-1",
                arguments={"q": "weather"},
            ) as tool:
                tool.set_output("sunny")

        span.update.assert_called_with(output={"content": "sunny"})

    def test_create_client_uses_langfuse_openai_wrapper(self):
        runtime = LangfuseObservabilityRuntime(
            {
                "public_key": "pk-lf-test",
                "secret_key": "sk-lf-test",
            }
        )
        fake_client = object()
        wrapped = MagicMock(return_value=fake_client)
        with patch.object(runtime, "_ensure_langfuse_client", return_value=SimpleNamespace()), patch(
            "langfuse.openai.AsyncOpenAI", wrapped
        ):
            client = runtime.create_client({"api_key": "sk-test", "base_url": "https://example.com"})

        self.assertIs(client, fake_client)
        wrapped.assert_called_once_with(api_key="sk-test", base_url="https://example.com")

    def test_ensure_langfuse_client_passes_constructor_kwargs(self):
        runtime = LangfuseObservabilityRuntime(
            {
                "public_key": "pk-lf-test",
                "secret_key": "sk-lf-test",
                "base_url": "https://us.cloud.langfuse.com",
                "sample_rate": 0.25,
                "debug": True,
                "tracing_enabled": False,
                "environment": "dev",
                "release": "1.2.3",
            }
        )
        constructed = MagicMock()
        with patch("langfuse.Langfuse", return_value=constructed) as langfuse_cls:
            client = runtime._ensure_langfuse_client()

        self.assertIs(client, constructed)
        langfuse_cls.assert_called_once_with(
            public_key="pk-lf-test",
            secret_key="sk-lf-test",
            base_url="https://us.cloud.langfuse.com",
            sample_rate=0.25,
            debug=True,
            tracing_enabled=False,
            environment="dev",
            release="1.2.3",
        )


if __name__ == "__main__":
    unittest.main()
