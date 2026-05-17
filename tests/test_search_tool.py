import unittest
from types import SimpleNamespace

from xagent.tools.search_tool import (
    DuckDuckGoHTMLParser,
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
        self.assertEqual(normalize_search_provider("ddg"), "duckduckgo")
        self.assertEqual(normalize_search_provider("openai_builtin"), "openai")
        self.assertEqual(normalize_search_provider("brave_search"), "brave")
        self.assertEqual(normalize_search_provider("off"), "none")

    def test_create_web_search_tool_returns_none_when_disabled(self):
        self.assertIsNone(create_web_search_tool({"provider": "none"}))

    async def test_brave_placeholder_key_returns_config_error(self):
        tool = create_web_search_tool({"provider": "brave", "api_key": "YOUR_API_KEY"})

        result = await tool(query="python", max_results=1)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["provider"], "brave")
        self.assertIn("API key", result["message"])

    def test_duckduckgo_html_parser_extracts_results(self):
        parser = DuckDuckGoHTMLParser()
        parser.feed(
            """
            <html><body>
              <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs">Example Docs</a>
              <a class="result__snippet">A useful documentation page.</a>
            </body></html>
            """
        )

        self.assertEqual(len(parser.results), 1)
        self.assertEqual(parser.results[0].title, "Example Docs")
        self.assertEqual(parser.results[0].url, "https://example.com/docs")
        self.assertEqual(parser.results[0].snippet, "A useful documentation page.")

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
            {"provider": "openai"},
            client=client,
            model="gpt-search",
        )

        result = await tool(query="python docs", max_results=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["answer"], "Result summary")
        self.assertEqual(result["results"], [{
            "title": "Example Docs",
            "url": "https://example.com/docs",
            "snippet": "",
        }])
        call = client.responses.calls[0]
        self.assertEqual(call["model"], "gpt-search")
        self.assertEqual(call["tools"][0]["type"], "web_search")
        self.assertEqual(call["tool_choice"], "auto")
        self.assertEqual(call["include"], ["web_search_call.action.sources"])


if __name__ == "__main__":
    unittest.main()