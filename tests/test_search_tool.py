import unittest
from types import SimpleNamespace

from xagent.tools.search_tool import (
    create_web_search_tool,
    normalize_search_provider,
)


class FakeResponses:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeOpenAIClient:
    def __init__(self, response):
        self.responses = FakeResponses(response)


class SearchToolTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_search_provider_aliases(self):
        self.assertEqual(normalize_search_provider("openai_builtin"), "openai")
        self.assertEqual(normalize_search_provider("dashscope"), "qwen")
        self.assertEqual(normalize_search_provider("qwen_search"), "qwen")
        self.assertEqual(normalize_search_provider("off"), "none")

    def test_unknown_search_provider_is_unsupported(self):
        with self.assertRaisesRegex(ValueError, "Unsupported search provider"):
            normalize_search_provider("unsupported_search")

    def test_create_web_search_tool_returns_none_when_disabled(self):
        self.assertIsNone(create_web_search_tool({"provider": "none"}))

    def test_openai_tool_schema_includes_only_openai_parameters(self):
        tool = create_web_search_tool({"provider": "openai"})
        properties = tool.tool_spec["function"]["parameters"]["properties"]

        self.assertIn("query", properties)
        self.assertIn("search_context_size", properties)
        self.assertIn("allowed_domains", properties)
        self.assertIn("external_web_access", properties)
        self.assertIn("force_search", properties)
        self.assertNotIn("enable_thinking", properties)
        self.assertNotIn("web_extractor", properties)
        self.assertNotIn("code_interpreter", properties)
        self.assertNotIn("freshness", properties)

    def test_qwen_tool_schema_includes_only_qwen_parameters(self):
        tool = create_web_search_tool({"provider": "qwen", "api_key": "qwen-key"})
        properties = tool.tool_spec["function"]["parameters"]["properties"]

        self.assertIn("query", properties)
        self.assertIn("max_results", properties)
        self.assertIn("enable_thinking", properties)
        self.assertIn("web_extractor", properties)
        self.assertIn("code_interpreter", properties)
        self.assertNotIn("search_context_size", properties)
        self.assertNotIn("allowed_domains", properties)
        self.assertNotIn("country", properties)
        self.assertNotIn("freshness", properties)

    async def test_qwen_placeholder_key_returns_config_error(self):
        tool = create_web_search_tool({"provider": "qwen", "api_key": "your_qwen_api_key_here"})

        result = await tool(query="杭州天气", max_results=1)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["provider"], "qwen")
        self.assertIn("Qwen search requires", result["message"])

    async def test_openai_search_rejects_qwen_only_parameters(self):
        tool = create_web_search_tool({"provider": "openai"}, client=FakeOpenAIClient(SimpleNamespace()))

        result = await tool(query="python docs", enable_thinking=True)

        self.assertEqual(result["status"], "error")
        self.assertIn("enable_thinking", result["message"])

    async def test_qwen_search_rejects_openai_only_parameters(self):
        tool = create_web_search_tool(
            {"provider": "qwen"},
            client=FakeOpenAIClient(SimpleNamespace()),
        )

        result = await tool(query="杭州天气", country="CN")

        self.assertEqual(result["status"], "error")
        self.assertIn("country", result["message"])

    async def test_openai_search_uses_responses_api(self):
        response = SimpleNamespace(
            output_text="Result summary",
            output=[
                {
                    "type": "message",
                    "content": [{
                        "text": "Result summary",
                        "annotations": [{
                            "type": "url_citation",
                            "url": "https://example.com/docs",
                            "title": "Example Docs",
                        }],
                    }],
                },
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [{
                            "url": "https://example.com/source",
                            "title": "Example Source",
                        }]
                    },
                },
            ],
        )
        client = FakeOpenAIClient(response)
        tool = create_web_search_tool(
            {
                "provider": "openai",
                "search_context_size": "high",
                "allowed_domains": ["https://example.com/docs"],
                "external_web_access": False,
                "return_token_budget": "unlimited",
            },
            client=client,
            model="gpt-search",
        )

        result = await tool(
            query="python docs",
            max_results=1,
            country="US",
            city="Seattle",
            force_search=True,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["answer"], "Result summary")
        self.assertEqual(result["results"], [{
            "title": "Example Docs",
            "url": "https://example.com/docs",
            "snippet": "",
        }])
        call = client.responses.calls[0]
        self.assertEqual(call["model"], "gpt-search")
        self.assertEqual(call["tool_choice"], "required")
        self.assertEqual(call["include"], ["web_search_call.action.sources"])
        self.assertEqual(call["tools"][0]["type"], "web_search")
        self.assertEqual(call["tools"][0]["search_context_size"], "high")
        self.assertEqual(call["tools"][0]["filters"], {"allowed_domains": ["example.com"]})
        self.assertEqual(call["tools"][0]["external_web_access"], False)
        self.assertEqual(call["tools"][0]["return_token_budget"], "unlimited")
        self.assertEqual(
            call["tools"][0]["user_location"],
            {"type": "approximate", "country": "US", "city": "Seattle"},
        )

    async def test_qwen_search_uses_responses_api_with_qwen_tools(self):
        response = SimpleNamespace(
            output_text="杭州今天有雨。",
            usage=SimpleNamespace(
                x_tools={
                    "web_search": {"count": 1},
                    "web_extractor": {"count": 1},
                    "code_interpreter": {"count": 1},
                }
            ),
            output=[
                {
                    "type": "message",
                    "content": [{
                        "text": "杭州今天有雨。",
                        "annotations": [{
                            "type": "url_citation",
                            "url": "https://weather.example.com/hangzhou",
                            "title": "Hangzhou Weather",
                        }],
                    }],
                },
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [{
                            "url": "https://weather.example.com/source",
                            "title": "Weather Source",
                        }]
                    },
                },
            ],
        )
        client = FakeOpenAIClient(response)
        tool = create_web_search_tool(
            {"provider": "qwen"},
            client=client,
            model="qwen3-max-2026-01-23",
        )

        result = await tool(query="杭州天气", max_results=2)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider"], "qwen")
        self.assertEqual(result["answer"], "杭州今天有雨。")
        self.assertEqual(result["tool_usage"], {"web_search": 1, "web_extractor": 1, "code_interpreter": 1})
        self.assertEqual(result["results"][0], {
            "title": "Hangzhou Weather",
            "url": "https://weather.example.com/hangzhou",
            "snippet": "",
        })
        call = client.responses.calls[0]
        self.assertEqual(call["model"], "qwen3-max-2026-01-23")
        self.assertEqual(
            [tool_config["type"] for tool_config in call["tools"]],
            ["web_search", "web_extractor", "code_interpreter"],
        )
        self.assertEqual(call["extra_body"], {"enable_thinking": True})
        self.assertNotIn("include", call)
        self.assertNotIn("tool_choice", call)

    async def test_qwen_search_can_disable_optional_tools(self):
        response = SimpleNamespace(output_text="ok", output=[])
        client = FakeOpenAIClient(response)
        tool = create_web_search_tool(
            {"provider": "qwen", "enable_thinking": False, "web_extractor": False, "code_interpreter": False},
            client=client,
        )

        result = await tool(query="杭州天气")

        self.assertEqual(result["status"], "ok")
        call = client.responses.calls[0]
        self.assertEqual(call["tools"], [{"type": "web_search"}])
        self.assertNotIn("extra_body", call)


if __name__ == "__main__":
    unittest.main()
