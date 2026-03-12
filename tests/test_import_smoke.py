import importlib
import sys
import unittest


class ImportSmokeTests(unittest.TestCase):
    def setUp(self):
        for module_name in [
            "xagent",
            "xagent.core",
            "xagent.core.agent",
            "xagent.interfaces",
            "xagent.interfaces.server",
            "xagent.interfaces.cli",
            "xagent.utils",
            "xagent.utils.mcp_convertor",
            "xagent.components.message.cloud_messages",
        ]:
            sys.modules.pop(module_name, None)

    def test_top_level_import_is_lazy(self):
        xagent = importlib.import_module("xagent")

        self.assertNotIn("xagent.core.agent", sys.modules)
        self.assertNotIn("xagent.interfaces.server", sys.modules)
        self.assertNotIn("xagent.utils.mcp_convertor", sys.modules)

        self.assertTrue(callable(xagent.Agent))
        self.assertIn("xagent.core.agent", sys.modules)
        self.assertNotIn("xagent.utils.mcp_convertor", sys.modules)
        self.assertNotIn("xagent.interfaces.server", sys.modules)

    def test_message_storage_import_keeps_optional_modules_unloaded(self):
        message_pkg = importlib.import_module("xagent.components.message")

        self.assertTrue(callable(message_pkg.MessageStorageLocal))
        self.assertNotIn("xagent.components.message.cloud_messages", sys.modules)
        self.assertNotIn("xagent.utils.mcp_convertor", sys.modules)

    def test_mcp_access_degrades_to_import_error(self):
        utils_pkg = importlib.import_module("xagent.utils")

        try:
            maybe_tool = utils_pkg.MCPTool
        except ImportError:
            maybe_tool = None
        except Exception as exc:  # pragma: no cover - defensive guard for regressions
            self.fail(f"Unexpected MCP import failure type: {type(exc).__name__}: {exc}")

        if maybe_tool is not None:
            self.assertTrue(callable(maybe_tool))
