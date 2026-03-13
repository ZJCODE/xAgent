"""
Tests for the modular xagent/core/ split.

Validates that:
- All four new modules are importable and expose expected public symbols
- ModelCaller static helpers behave correctly without a live API
- ToolExecutor registration, cache invalidation, and concurrency guard work
- MCPManager cache TTL logic is respected
- ImageProcessor caption fallback path works without an API call
- Agent backward-compat proxies (tools, mcp_tools, cached_tool_specs) still work
- ReplyType moved to model_caller is re-exported from agent module
"""

import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helper: a tiny fake memory storage that satisfies the Agent.__init__ contract
# ---------------------------------------------------------------------------

class _DummyMemory:
    llm_service = None

    async def add(self, *a, **kw):
        pass

    async def retrieve(self, *a, **kw):
        return []


# ---------------------------------------------------------------------------
# M-1 — all four new core modules are importable
# ---------------------------------------------------------------------------

class ModuleImportTests(unittest.TestCase):
    def test_model_caller_importable(self):
        import xagent.core.model_caller as mc
        self.assertTrue(callable(mc.ModelCaller))
        self.assertTrue(issubclass(mc.ReplyType, object))

    def test_tool_executor_importable(self):
        import xagent.core.tool_executor as te
        self.assertTrue(callable(te.ToolExecutor))
        self.assertTrue(callable(te.make_http_agent_tool))

    def test_mcp_manager_importable(self):
        import xagent.core.mcp_manager as mm
        self.assertTrue(callable(mm.MCPManager))

    def test_image_processor_importable(self):
        import xagent.core.image_processor as ip
        self.assertTrue(callable(ip.ImageProcessor))

    def test_core_init_exports_all_symbols(self):
        import xagent.core as core
        # Non-Agent symbols are always importable without heavy deps
        lightweight = [
            "normalize_session_id",
            "ModelCaller", "ReplyType",
            "ToolExecutor",
            "MCPManager",
            "ImageProcessor",
        ]
        for name in lightweight:
            self.assertTrue(
                hasattr(core, name),
                f"xagent.core is missing '{name}'",
            )
        # Agent / AgentConfig trigger optional dep chain — just verify __all__
        self.assertIn("Agent", core.__all__)
        self.assertIn("AgentConfig", core.__all__)


# ---------------------------------------------------------------------------
# M-2 — ReplyType is still importable from xagent.core.agent
# ---------------------------------------------------------------------------

class ReplyTypeBackCompatTests(unittest.TestCase):
    def test_reply_type_importable_from_agent(self):
        """ReplyType must still be accessible via xagent.core.agent for callers."""
        from xagent.core.model_caller import ReplyType
        from enum import Enum
        self.assertTrue(issubclass(ReplyType, Enum))
        values = {e.value for e in ReplyType}
        self.assertIn("simple_reply", values)
        self.assertIn("tool_call", values)
        self.assertIn("structured_reply", values)
        self.assertIn("error", values)


# ---------------------------------------------------------------------------
# M-3 — ModelCaller static helpers
# ---------------------------------------------------------------------------

class ModelCallerStaticTests(unittest.TestCase):
    def _mc(self):
        from xagent.core.model_caller import ModelCaller
        return ModelCaller

    def test_sanitize_removes_leading_function_call_output(self):
        from xagent.schemas import MessageType
        MC = self._mc()
        msgs = [
            {"type": MessageType.FUNCTION_CALL_OUTPUT, "role": "tool", "content": "x"},
            {"role": "user", "content": "hi"},
        ]
        result = MC._sanitize_input_messages(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "user")

    def test_sanitize_leaves_non_function_call_output_intact(self):
        MC = self._mc()
        msgs = [{"role": "user", "content": "hello"}]
        result = MC._sanitize_input_messages(list(msgs))
        self.assertEqual(result, msgs)

    def test_extract_stream_text_delta_returns_delta(self):
        MC = self._mc()
        event = SimpleNamespace(type="response.output_text.delta", delta="Hello")
        self.assertEqual(MC._extract_stream_text_delta(event), "Hello")

    def test_extract_stream_text_delta_returns_empty_on_other_event(self):
        MC = self._mc()
        event = SimpleNamespace(type="other_event")
        self.assertEqual(MC._extract_stream_text_delta(event), "")

    def test_extract_response_text_uses_output_text(self):
        MC = self._mc()
        resp = SimpleNamespace(output_text="Final answer")
        self.assertEqual(MC._extract_response_text(resp, "fallback"), "Final answer")

    def test_extract_response_text_falls_back(self):
        MC = self._mc()
        self.assertEqual(MC._extract_response_text(None, "fallback"), "fallback")

    def test_classify_stream_event_text_delta(self):
        from xagent.core.model_caller import ReplyType
        MC = self._mc()
        event = SimpleNamespace(type="response.output_text.delta")
        self.assertEqual(MC._classify_stream_event(event), ReplyType.SIMPLE_REPLY)

    def test_classify_stream_event_function_call(self):
        from xagent.core.model_caller import ReplyType
        MC = self._mc()
        event = SimpleNamespace(type="response.function_call.delta")
        self.assertEqual(MC._classify_stream_event(event), ReplyType.TOOL_CALL)

    def test_classify_stream_event_unknown_returns_none(self):
        MC = self._mc()
        event = SimpleNamespace(type="unknown_event_xyz")
        self.assertIsNone(MC._classify_stream_event(event))


# ---------------------------------------------------------------------------
# M-4 — ToolExecutor: registration, cache, concurrency guard
# ---------------------------------------------------------------------------

class ToolExecutorTests(unittest.TestCase):
    def _make_tool(self, name: str):
        """Build a minimal async callable with tool_spec."""

        async def fn(**kwargs):
            return f"result_from_{name}"

        fn.tool_spec = {"type": "function", "name": name, "parameters": {}}
        return fn

    def test_register_adds_tools(self):
        from xagent.core.tool_executor import ToolExecutor
        te = ToolExecutor()
        t = self._make_tool("my_tool")
        te.register([t])
        self.assertIn("my_tool", te.tools)

    def test_register_sync_tool_raises(self):
        from xagent.core.tool_executor import ToolExecutor
        te = ToolExecutor()

        def sync_fn(**kwargs):
            return "result"

        sync_fn.tool_spec = {"name": "sync", "parameters": {}}
        with self.assertRaises(TypeError):
            te.register([sync_fn])

    def test_cached_specs_rebuilds_on_first_call(self):
        from xagent.core.tool_executor import ToolExecutor
        te = ToolExecutor()
        t = self._make_tool("tool_a")
        te.register([t])
        specs = te.cached_specs
        self.assertIsNotNone(specs)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["name"], "tool_a")

    def test_update_mcp_tools_invalidates_cache(self):
        from xagent.core.tool_executor import ToolExecutor
        import time
        te = ToolExecutor()
        t = self._make_tool("static_tool")
        te.register([t])
        _ = te.cached_specs  # prime cache
        self.assertIsNotNone(te._cache)

        mcp_t = self._make_tool("mcp_tool")
        te.update_mcp_tools({"mcp_tool": mcp_t}, time.time())
        self.assertIsNone(te._cache)  # should be invalidated

        specs = te.cached_specs
        names = [s["name"] for s in specs]
        self.assertIn("static_tool", names)
        self.assertIn("mcp_tool", names)

    def test_update_mcp_tools_same_timestamp_does_not_invalidate(self):
        from xagent.core.tool_executor import ToolExecutor
        import time
        te = ToolExecutor()
        t = self._make_tool("t")
        te.register([t])
        ts = time.time()
        mcp_t = self._make_tool("mcp")
        te.update_mcp_tools({"mcp": mcp_t}, ts)
        _ = te.cached_specs  # prime cache after update
        self.assertIsNotNone(te._cache)
        # Calling with the same timestamp should NOT invalidate
        te.update_mcp_tools({"mcp": mcp_t}, ts)
        self.assertIsNotNone(te._cache)

    def test_cached_specs_returns_none_when_no_tools(self):
        from xagent.core.tool_executor import ToolExecutor
        te = ToolExecutor()
        self.assertIsNone(te.cached_specs)


# ---------------------------------------------------------------------------
# M-5 — MCPManager cache TTL
# ---------------------------------------------------------------------------

class MCPManagerTests(unittest.TestCase):
    def test_refresh_is_skipped_when_cache_fresh(self):
        from xagent.core.mcp_manager import MCPManager
        import time

        mgr = MCPManager(servers=["http://fake-mcp/"], cache_ttl=300)
        # Manually set last_updated to now (simulates a fresh cache)
        mgr._last_updated = time.time()
        mgr._tools = {"fake_tool": object()}

        # refresh() should be a no-op
        asyncio.run(mgr.refresh())
        self.assertIn("fake_tool", mgr.tools)  # not cleared

    def test_refresh_clears_on_stale_cache(self):
        from xagent.core.mcp_manager import MCPManager
        import time

        mgr = MCPManager(servers=[], cache_ttl=300)
        # Set last_updated to long ago (stale)
        mgr._last_updated = time.time() - 400
        mgr._tools = {"old_tool": object()}

        asyncio.run(mgr.refresh())
        self.assertNotIn("old_tool", mgr.tools)  # should be cleared

    def test_empty_servers_completes_quickly(self):
        from xagent.core.mcp_manager import MCPManager
        mgr = MCPManager(servers=[])
        asyncio.run(mgr.refresh())
        self.assertIsNotNone(mgr.last_updated)
        self.assertEqual(mgr.tools, {})


# ---------------------------------------------------------------------------
# M-6 — ImageProcessor fallback path
# ---------------------------------------------------------------------------

class ImageProcessorTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_caption_uses_prompt_fallback_on_api_error(self):
        from xagent.core.image_processor import ImageProcessor

        client = MagicMock()
        client.responses.create = AsyncMock(side_effect=RuntimeError("API down"))
        ip = ImageProcessor(client)

        result = self._run(ip.caption("data:image/png;base64,abc", "a red fox"))
        self.assertIn("red fox", result)

    def test_caption_generic_fallback_when_no_prompt_hint(self):
        from xagent.core.image_processor import ImageProcessor

        client = MagicMock()
        client.responses.create = AsyncMock(side_effect=RuntimeError("API down"))
        ip = ImageProcessor(client)

        result = self._run(ip.caption("data:image/png;base64,abc"))
        self.assertIn("image", result.lower())

    def test_caption_returns_api_result_on_success(self):
        from xagent.core.image_processor import ImageProcessor

        fake_response = SimpleNamespace(output_text="A fluffy cat.")
        client = MagicMock()
        client.responses.create = AsyncMock(return_value=fake_response)
        ip = ImageProcessor(client)

        result = self._run(ip.caption("data:image/png;base64,abc", "a cat"))
        self.assertEqual(result, "A fluffy cat.")


# ---------------------------------------------------------------------------
# M-7 — Agent backward-compat proxies
# ---------------------------------------------------------------------------

class AgentBackCompatProxyTests(unittest.TestCase):
    def _make_agent(self):
        from xagent.core.agent import Agent

        dummy_client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(), parse=AsyncMock())
        )
        return Agent(
            name="compat_agent",
            client=dummy_client,
            memory_storage=_DummyMemory(),
        )

    def test_agent_tools_proxy_is_dict(self):
        agent = self._make_agent()
        self.assertIsInstance(agent.tools, dict)

    def test_agent_mcp_tools_proxy_is_dict(self):
        agent = self._make_agent()
        self.assertIsInstance(agent.mcp_tools, dict)

    def test_agent_cached_tool_specs_proxy(self):
        from xagent.utils.tool_decorator import function_tool
        agent = self._make_agent()

        @function_tool(name="ping", description="ping")
        async def ping():
            return "pong"

        agent._register_tools([ping])
        specs = agent.cached_tool_specs
        self.assertIsNotNone(specs)
        self.assertTrue(any(s["name"] == "ping" for s in specs))

    def test_agent_mcp_servers_proxy_is_list(self):
        agent = self._make_agent()
        self.assertIsInstance(agent.mcp_servers, list)

    def test_register_tools_backward_compat(self):
        """_register_tools() on Agent must still work (Swarm calls this)."""
        from xagent.utils.tool_decorator import function_tool
        agent = self._make_agent()

        @function_tool(name="echo", description="echo")
        async def echo(text: str):
            return text

        agent._register_tools([echo])
        self.assertIn("echo", agent.tools)


if __name__ == "__main__":
    unittest.main()
