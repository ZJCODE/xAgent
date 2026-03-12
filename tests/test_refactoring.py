"""
Tests that validate the Linux-philosophy refactoring:
- H1: No logging.basicConfig() in library code
- H2: CORS wildcard triggers SecurityWarning
- H3: upstash_vector_store.py (corrected spelling) exists and is importable
- H5: AgentInput Field bounds reject out-of-range values
- M1: Langfuse is optional — xagent.observability works without langfuse
- M5: TOOL_RESULT_PREVIEW_LENGTH is 200
- M8: xagent.defaults exists with expected constants
- L1: Swarm is importable and initialises correctly
- L2: YAML dependency parsing works
"""

import importlib
import sys
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# H1 — library code must NOT call logging.basicConfig()
# ---------------------------------------------------------------------------

class LoggingConfigTests(unittest.TestCase):
    def test_agent_module_does_not_call_logging_basicConfig(self):
        """Importing agent.py must not configure the root logger."""
        import logging

        # Record root-logger handlers before import
        root = logging.getLogger()
        handlers_before = list(root.handlers)

        # Force re-import of agent module
        for mod in list(sys.modules):
            if mod.startswith("xagent.core.agent"):
                del sys.modules[mod]

        importlib.import_module("xagent.core.agent")

        # Root handlers must not have grown (basicConfig adds a StreamHandler)
        self.assertEqual(
            len(root.handlers),
            len(handlers_before),
            "agent.py must not call logging.basicConfig() — it modifies the root logger.",
        )


# ---------------------------------------------------------------------------
# H2 — CORS wildcard origins trigger SecurityWarning
# ---------------------------------------------------------------------------

class CORSSecurityTests(unittest.TestCase):
    def test_wildcard_cors_emits_security_warning(self):
        from xagent.interfaces.server import AgentHTTPServer, SecurityWarning

        dummy_client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(), parse=AsyncMock())
        )
        from xagent.core.agent import Agent

        class _DummyMemory:
            async def add(self, *a, **kw): pass
            async def retrieve(self, *a, **kw): return []
            llm_service = None

        agent = Agent(
            name="h2_test",
            client=dummy_client,
            memory_storage=_DummyMemory(),
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            AgentHTTPServer(agent=agent, enable_web=False)

        security_warnings = [w for w in caught if issubclass(w.category, SecurityWarning)]
        self.assertTrue(
            len(security_warnings) >= 1,
            "Expected SecurityWarning when CORS allow_origins=['*'] is used.",
        )


# ---------------------------------------------------------------------------
# H3 — corrected spelling: upstash_vector_store.py must exist
# ---------------------------------------------------------------------------

class UpstashSpellingTests(unittest.TestCase):
    def test_correctly_named_upstash_file_exists(self):
        import os
        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "xagent", "components", "memory", "vector_store",
            "upstash_vector_store.py",
        )
        self.assertTrue(
            os.path.isfile(os.path.normpath(path)),
            "upstash_vector_store.py (correct spelling) must exist.",
        )

    def test_typo_file_is_just_a_compat_shim(self):
        """The old upstach file should be a thin re-export, not the real implementation."""
        import os
        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "xagent", "components", "memory", "vector_store",
            "upstach_vector_store.py",
        )
        content = open(os.path.normpath(path)).read()
        self.assertIn("upstash_vector_store", content,
                      "upstach file must re-export from upstash_vector_store.py")
        # The typo file should NOT contain the full class definition
        self.assertNotIn("class VectorStoreUpstash(VectorStoreBase):", content,
                         "upstach file must not contain the full implementation")


# ---------------------------------------------------------------------------
# H5 — AgentInput Field bounds
# ---------------------------------------------------------------------------

class AgentInputBoundsTests(unittest.TestCase):
    def test_max_iter_must_be_positive(self):
        from pydantic import ValidationError
        from xagent.interfaces.server import AgentInput

        with self.assertRaises(ValidationError):
            AgentInput(user_id="u", session_id="s", user_message="hi", max_iter=0)

    def test_max_iter_upper_bound(self):
        from pydantic import ValidationError
        from xagent.interfaces.server import AgentInput
        from xagent.defaults import API_MAX_ITER_LIMIT

        with self.assertRaises(ValidationError):
            AgentInput(user_id="u", session_id="s", user_message="hi", max_iter=API_MAX_ITER_LIMIT + 1)

    def test_max_concurrent_tools_upper_bound(self):
        from pydantic import ValidationError
        from xagent.interfaces.server import AgentInput
        from xagent.defaults import API_MAX_CONCURRENT_TOOLS_LIMIT

        with self.assertRaises(ValidationError):
            AgentInput(user_id="u", session_id="s", user_message="hi",
                       max_concurrent_tools=API_MAX_CONCURRENT_TOOLS_LIMIT + 1)

    def test_history_count_upper_bound(self):
        from pydantic import ValidationError
        from xagent.interfaces.server import AgentInput
        from xagent.defaults import API_MAX_HISTORY_COUNT_LIMIT

        with self.assertRaises(ValidationError):
            AgentInput(user_id="u", session_id="s", user_message="hi",
                       history_count=API_MAX_HISTORY_COUNT_LIMIT + 1)

    def test_valid_values_are_accepted(self):
        from xagent.interfaces.server import AgentInput

        inp = AgentInput(user_id="u", session_id="s", user_message="hi",
                         max_iter=10, max_concurrent_tools=5, history_count=20)
        self.assertEqual(inp.max_iter, 10)


# ---------------------------------------------------------------------------
# M1 — xagent.observability works without langfuse
# ---------------------------------------------------------------------------

class ObservabilityTests(unittest.TestCase):
    def test_observe_decorator_is_callable_without_langfuse(self):
        """observe() must behave as a no-op when langfuse is absent."""
        from xagent.observability import observe

        @observe
        async def dummy():
            return "ok"

        self.assertTrue(callable(dummy))

    def test_get_openai_client_returns_without_langfuse(self):
        """get_openai_client() returns a standard AsyncOpenAI when langfuse absent."""
        import xagent.observability as obs
        from unittest.mock import patch, MagicMock

        # Simulate langfuse not installed
        orig = obs._LANGFUSE_AVAILABLE
        orig_cls = obs._LangfuseAsyncOpenAI
        obs._LANGFUSE_AVAILABLE = False
        obs._LangfuseAsyncOpenAI = None
        try:
            mock_client = MagicMock()
            # AsyncOpenAI is imported lazily inside get_openai_client()
            with patch("openai.AsyncOpenAI", return_value=mock_client) as patched:
                client = obs.get_openai_client()
                patched.assert_called_once()
                self.assertIs(client, mock_client)
        finally:
            obs._LANGFUSE_AVAILABLE = orig
            obs._LangfuseAsyncOpenAI = orig_cls


# ---------------------------------------------------------------------------
# M5 — TOOL_RESULT_PREVIEW_LENGTH == 200
# ---------------------------------------------------------------------------

class DefaultsTests(unittest.TestCase):
    def test_tool_result_preview_length_is_200(self):
        from xagent import defaults
        self.assertEqual(defaults.TOOL_RESULT_PREVIEW_LENGTH, 200)

    def test_defaults_module_exports_all_expected_names(self):
        from xagent import defaults
        expected = [
            "DEFAULT_HISTORY_COUNT",
            "DEFAULT_MAX_ITER",
            "DEFAULT_MAX_CONCURRENT_TOOLS",
            "TOOL_RESULT_PREVIEW_LENGTH",
            "MCP_CACHE_TTL",
            "HTTP_TIMEOUT",
            "MEMORY_BUFFER_THRESHOLD",
            "MEMORY_KEEP_RECENT",
            "MEMORY_TTL_SECONDS",
            "MEMORY_RETRIEVAL_LIMIT",
            "LOCAL_BUFFER_MAX_SIZE",
            "WORKFLOW_MAX_CONCURRENT",
            "API_MAX_ITER_LIMIT",
            "API_MAX_CONCURRENT_TOOLS_LIMIT",
            "API_MAX_HISTORY_COUNT_LIMIT",
        ]
        for name in expected:
            self.assertTrue(hasattr(defaults, name), f"defaults.{name} is missing")


# ---------------------------------------------------------------------------
# L1 — Swarm is importable and initialises
# ---------------------------------------------------------------------------

class SwarmTests(unittest.TestCase):
    def test_swarm_is_importable_from_xagent(self):
        import xagent
        self.assertTrue(callable(xagent.Swarm))

    def test_swarm_init_registers_agents_as_tools(self):
        from xagent.multi.swarm import Swarm
        from xagent.core.agent import Agent

        dummy_client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(), parse=AsyncMock())
        )

        class _DummyMemory:
            async def add(self, *a, **kw): pass
            async def retrieve(self, *a, **kw): return []
            llm_service = None

        a1 = Agent(name="spec1", description="First specialist",
                   client=dummy_client, memory_storage=_DummyMemory())
        a2 = Agent(name="spec2", description="Second specialist",
                   client=dummy_client, memory_storage=_DummyMemory())

        # Provide a coordinator with a dummy client so we don't need a real API key
        coordinator = Agent(name="coordinator", client=dummy_client,
                            memory_storage=_DummyMemory())
        swarm = Swarm(agents=[a1, a2], coordinator=coordinator)
        self.assertIsNotNone(swarm.coordinator)
        # Coordinator must have the specialist tools registered
        self.assertIn("spec1", swarm.coordinator.tools)
        self.assertIn("spec2", swarm.coordinator.tools)

    def test_swarm_requires_at_least_one_agent(self):
        from xagent.multi.swarm import Swarm

        with self.assertRaises(ValueError):
            Swarm(agents=[])


# ---------------------------------------------------------------------------
# L2 — YAML dependency parsing
# ---------------------------------------------------------------------------

class YAMLDSLTests(unittest.TestCase):
    def test_parse_dependencies_yaml_basic(self):
        from xagent.utils.workflow_dsl import parse_dependencies_yaml

        yaml_str = """
dependencies:
  B:
    - A
  C:
    - A
    - B
"""
        result = parse_dependencies_yaml(yaml_str)
        self.assertEqual(result, {"B": ["A"], "C": ["A", "B"]})

    def test_parse_dependencies_yaml_empty(self):
        from xagent.utils.workflow_dsl import parse_dependencies_yaml

        self.assertEqual(parse_dependencies_yaml(""), {})
        self.assertEqual(parse_dependencies_yaml("   "), {})

    def test_parse_dependencies_yaml_no_deps_node(self):
        from xagent.utils.workflow_dsl import parse_dependencies_yaml

        yaml_str = """
dependencies:
  A:
"""
        result = parse_dependencies_yaml(yaml_str)
        self.assertEqual(result, {"A": []})

    def test_graph_workflow_accepts_yaml_string(self):
        from xagent.multi.workflow import GraphWorkflow
        from xagent.core.agent import Agent

        dummy_client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(), parse=AsyncMock())
        )

        class _DummyMemory:
            async def add(self, *a, **kw): pass
            async def retrieve(self, *a, **kw): return []
            llm_service = None

        a = Agent(name="A", client=dummy_client, memory_storage=_DummyMemory())
        b = Agent(name="B", client=dummy_client, memory_storage=_DummyMemory())

        yaml_deps = "dependencies:\n  B:\n    - A\n"
        wf = GraphWorkflow(agents=[a, b], dependencies=yaml_deps)
        self.assertEqual(wf.dependencies, {"B": ["A"]})


if __name__ == "__main__":
    unittest.main()
