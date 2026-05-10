import unittest

from xagent.tools.search_tool import (
    DuckDuckGoHTMLParser,
    create_web_search_tool,
    normalize_search_provider,
)


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


if __name__ == "__main__":
    unittest.main()