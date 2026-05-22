"""Configurable web search tool implementations."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from openai import AsyncOpenAI

from xagent.core.config import AgentConfig
from xagent.utils.tool_decorator import function_tool


logger = logging.getLogger(__name__)

SEARCH_PROVIDER_OPENAI = "openai"
SEARCH_PROVIDER_DUCKDUCKGO = "duckduckgo"
SEARCH_PROVIDER_BRAVE = "brave"
SEARCH_PROVIDER_NONE = "none"
SUPPORTED_SEARCH_PROVIDERS = {
    SEARCH_PROVIDER_OPENAI,
    SEARCH_PROVIDER_DUCKDUCKGO,
    SEARCH_PROVIDER_BRAVE,
    SEARCH_PROVIDER_NONE,
}

BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
DUCKDUCKGO_INSTANT_ANSWER_ENDPOINT = "https://api.duckduckgo.com/"
DUCKDUCKGO_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
BRAVE_API_KEY_ENV_VARS = ("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY")
PLACEHOLDER_API_KEYS = {
    "your_api_key",
    "your_api_key_here",
    "your_openai_api_key",
    "your_openai_api_key_here",
    "your_minimax_api_key",
    "your_minimax_api_key_here",
    "your_qwen_api_key",
    "your_qwen_api_key_here",
    "your_dashscope_api_key",
    "your_dashscope_api_key_here",
    "your_brave_search_api_key",
}


@dataclass(frozen=True)
class SearchResult:
    """Normalized search result returned to the agent."""

    title: str
    url: str
    snippet: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
        }


class DuckDuckGoHTMLParser(HTMLParser):
    """Extract organic results from DuckDuckGo's HTML endpoint."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._capturing_title = False
        self._capturing_snippet = False
        self._current_title_parts: list[str] = []
        self._current_snippet_parts: list[str] = []
        self._current_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attributes = dict(attrs)
        class_name = attributes.get("class") or ""

        if tag == "a" and "result__a" in class_name:
            self._capturing_title = True
            self._current_title_parts = []
            self._current_url = _decode_duckduckgo_url(attributes.get("href") or "")
            return

        if "result__snippet" in class_name:
            self._capturing_snippet = True
            self._current_snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing_title:
            self._current_title_parts.append(data)
        elif self._capturing_snippet:
            self._current_snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capturing_title and tag == "a":
            self._capturing_title = False
            title = _clean_text(" ".join(self._current_title_parts))
            if title and self._current_url:
                self.results.append(SearchResult(title=title, url=self._current_url))

        if self._capturing_snippet and tag in {"a", "div"}:
            self._capturing_snippet = False
            snippet = _clean_text(" ".join(self._current_snippet_parts))
            if snippet and self.results:
                latest = self.results[-1]
                self.results[-1] = SearchResult(
                    title=latest.title,
                    url=latest.url,
                    snippet=snippet,
                )


@dataclass(frozen=True)
class ConfiguredSearchProvider:
    """Dispatch web search calls to the configured provider."""

    provider: str
    config: dict
    client: Optional[AsyncOpenAI]
    model: Optional[str]

    async def search(
        self,
        query: str,
        max_results: int = 5,
        freshness: Optional[str] = None,
        country: Optional[str] = None,
        search_lang: Optional[str] = None,
    ) -> dict:
        query = query.strip()
        if not query:
            return _error_response(self.provider, query, "query is required")

        result_limit = _normalize_result_limit(max_results)
        if self.provider == SEARCH_PROVIDER_OPENAI:
            return await self._search_openai(query, result_limit, freshness, country, search_lang)
        if self.provider == SEARCH_PROVIDER_DUCKDUCKGO:
            return await self._search_duckduckgo(query, result_limit, freshness, country, search_lang)
        if self.provider == SEARCH_PROVIDER_BRAVE:
            return await self._search_brave(query, result_limit, freshness, country, search_lang)
        return _error_response(self.provider, query, "search is disabled")

    async def _search_openai(
        self,
        query: str,
        result_limit: int,
        freshness: Optional[str],
        country: Optional[str],
        search_lang: Optional[str],
    ) -> dict:
        search_client = self.client
        if search_client is None:
            try:
                search_client = AsyncOpenAI()
            except Exception as exception:
                return _error_response(
                    self.provider,
                    query,
                    f"OpenAI client is not configured: {exception}",
                )

        tool_config: dict[str, Any] = {
            "type": "web_search",
            "search_context_size": "medium",
        }
        normalized_country = _normalize_country(country)
        if normalized_country:
            tool_config["user_location"] = {
                "type": "approximate",
                "country": normalized_country,
            }

        input_text = _build_openai_search_input(query, result_limit, freshness, search_lang)
        try:
            response = await search_client.responses.create(
                model=self.model or AgentConfig.DEFAULT_MODEL,
                tools=[tool_config],
                tool_choice="auto",
                include=["web_search_call.action.sources"],
                input=input_text,
            )
        except Exception as exception:
            logger.warning("OpenAI web search failed: %s", exception)
            return _error_response(self.provider, query, str(exception))

        answer = _extract_openai_output_text(response)
        citation_results = _extract_openai_citation_results(response)
        source_results = _extract_openai_source_results(response)
        results = _deduplicate_results(citation_results + source_results)[:result_limit]
        return {
            "status": "ok",
            "provider": self.provider,
            "query": query,
            "answer": answer,
            "results": [result.to_dict() for result in results],
        }

    async def _search_duckduckgo(
        self,
        query: str,
        result_limit: int,
        freshness: Optional[str],
        country: Optional[str],
        search_lang: Optional[str],
    ) -> dict:
        headers = {
            "User-Agent": "xAgent/1.0 (+https://github.com/ZJCODE/xagent)",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        }
        results: list[SearchResult] = []
        errors: list[str] = []

        async with httpx.AsyncClient(
            timeout=AgentConfig.SEARCH_HTTP_TIMEOUT,
            headers=headers,
            follow_redirects=True,
        ) as http_client:
            try:
                results.extend(await _fetch_duckduckgo_instant_answer(http_client, query))
            except Exception as exception:
                logger.debug("DuckDuckGo instant answer failed: %s", exception)
                errors.append(str(exception))

            if len(results) < result_limit:
                try:
                    html_results = await _fetch_duckduckgo_html_results(
                        http_client,
                        query,
                        freshness=freshness,
                        country=country,
                        search_lang=search_lang,
                    )
                    results.extend(html_results)
                except Exception as exception:
                    logger.debug("DuckDuckGo HTML search failed: %s", exception)
                    errors.append(str(exception))

        normalized_results = _deduplicate_results(results)[:result_limit]
        status = "ok" if normalized_results else "empty"
        response = {
            "status": status,
            "provider": self.provider,
            "query": query,
            "results": [result.to_dict() for result in normalized_results],
        }
        if errors and not normalized_results:
            response["message"] = "; ".join(errors)
        return response

    async def _search_brave(
        self,
        query: str,
        result_limit: int,
        freshness: Optional[str],
        country: Optional[str],
        search_lang: Optional[str],
    ) -> dict:
        api_key = _get_brave_api_key(self.config)
        if is_placeholder_api_key(api_key):
            return _error_response(
                self.provider,
                query,
                "Brave Search API key is required. Set search.api_key or BRAVE_SEARCH_API_KEY.",
            )

        params: dict[str, Any] = {
            "q": query,
            "count": min(result_limit, 20),
            "safesearch": self.config.get("safesearch", "moderate"),
            "extra_snippets": "true",
        }
        normalized_freshness = _normalize_brave_freshness(freshness)
        normalized_country = _normalize_country(country)
        if normalized_freshness:
            params["freshness"] = normalized_freshness
        if normalized_country:
            params["country"] = normalized_country
        if search_lang:
            params["search_lang"] = search_lang.strip().lower()

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=AgentConfig.SEARCH_HTTP_TIMEOUT) as http_client:
                response = await http_client.get(BRAVE_SEARCH_ENDPOINT, params=params, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as exception:
            logger.warning("Brave web search failed: %s", exception)
            return _error_response(self.provider, query, str(exception))

        results = _extract_brave_results(payload)[:result_limit]
        query_info = payload.get("query") or {}
        return {
            "status": "ok" if results else "empty",
            "provider": self.provider,
            "query": query_info.get("original") or query,
            "more_results_available": bool(query_info.get("more_results_available")),
            "results": [result.to_dict() for result in results],
        }


def create_web_search_tool(
    search_config: Optional[dict],
    *,
    client: Optional[AsyncOpenAI] = None,
    model: Optional[str] = None,
):
    """Create the configured web_search tool, or return None when disabled."""
    config = search_config or {}
    provider = normalize_search_provider(config.get("provider"))
    if provider == SEARCH_PROVIDER_NONE:
        return None

    search_provider = ConfiguredSearchProvider(
        provider=provider,
        config=config,
        client=client,
        model=model,
    )

    @function_tool(
        name="web_search",
        description=(
            "Search the web for current or external information using the configured provider. "
            "Return concise results with source URLs so the final answer can cite them."
        ),
        param_descriptions={
            "query": "Search query. Include key entities, dates, and constraints.",
            "max_results": "Maximum number of results to return, from 1 to 20. Defaults to 5.",
            "freshness": "Optional freshness hint: day, week, month, year, or provider-specific value.",
            "country": "Optional two-letter country code such as US, CN, GB, or DE.",
            "search_lang": "Optional two-letter search language code such as en, zh, ja, or de.",
        },
    )
    async def web_search(
        query: str,
        max_results: int = 5,
        freshness: Optional[str] = None,
        country: Optional[str] = None,
        search_lang: Optional[str] = None,
    ) -> dict:
        return await search_provider.search(
            query=query,
            max_results=max_results,
            freshness=freshness,
            country=country,
            search_lang=search_lang,
        )

    return web_search


def normalize_search_provider(provider: Any) -> str:
    normalized = str(provider or SEARCH_PROVIDER_NONE).strip().lower().replace("-", "_")
    aliases = {
        "off": SEARCH_PROVIDER_NONE,
        "disabled": SEARCH_PROVIDER_NONE,
        "no_search": SEARCH_PROVIDER_NONE,
        "none": SEARCH_PROVIDER_NONE,
        "openai_builtin": SEARCH_PROVIDER_OPENAI,
        "openai_web_search": SEARCH_PROVIDER_OPENAI,
        "ddg": SEARCH_PROVIDER_DUCKDUCKGO,
        "duck_duck_go": SEARCH_PROVIDER_DUCKDUCKGO,
        "duckduckgo": SEARCH_PROVIDER_DUCKDUCKGO,
        "brave_search": SEARCH_PROVIDER_BRAVE,
        "brave": SEARCH_PROVIDER_BRAVE,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_SEARCH_PROVIDERS:
        raise ValueError(f"Unsupported search provider: {provider}")
    return normalized


async def _fetch_duckduckgo_instant_answer(
    http_client: httpx.AsyncClient,
    query: str,
) -> list[SearchResult]:
    response = await http_client.get(
        DUCKDUCKGO_INSTANT_ANSWER_ENDPOINT,
        params={
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        },
    )
    response.raise_for_status()
    payload = response.json()

    results: list[SearchResult] = []
    abstract = _clean_text(payload.get("AbstractText") or "")
    abstract_url = payload.get("AbstractURL") or ""
    if abstract and abstract_url:
        results.append(
            SearchResult(
                title=_clean_text(payload.get("Heading") or query),
                url=abstract_url,
                snippet=abstract,
            )
        )

    for item in _iter_duckduckgo_topics(payload.get("Results") or []):
        result = _duckduckgo_topic_to_result(item)
        if result:
            results.append(result)
    for item in _iter_duckduckgo_topics(payload.get("RelatedTopics") or []):
        result = _duckduckgo_topic_to_result(item)
        if result:
            results.append(result)
    return results


async def _fetch_duckduckgo_html_results(
    http_client: httpx.AsyncClient,
    query: str,
    *,
    freshness: Optional[str],
    country: Optional[str],
    search_lang: Optional[str],
) -> list[SearchResult]:
    params: dict[str, str] = {"q": query}
    region = _duckduckgo_region(country, search_lang)
    freshness_value = _normalize_duckduckgo_freshness(freshness)
    if region:
        params["kl"] = region
    if freshness_value:
        params["df"] = freshness_value

    response = await http_client.get(DUCKDUCKGO_HTML_ENDPOINT, params=params)
    response.raise_for_status()
    parser = DuckDuckGoHTMLParser()
    parser.feed(response.text)
    return parser.results


def _iter_duckduckgo_topics(items: list[dict]) -> list[dict]:
    topics: list[dict] = []
    for item in items:
        nested_topics = item.get("Topics")
        if isinstance(nested_topics, list):
            topics.extend(_iter_duckduckgo_topics(nested_topics))
        else:
            topics.append(item)
    return topics


def _duckduckgo_topic_to_result(item: dict) -> Optional[SearchResult]:
    title = _clean_text(item.get("Text") or "")
    url = item.get("FirstURL") or ""
    if not title or not url:
        return None
    return SearchResult(title=title, url=url, snippet=title)


def _extract_brave_results(payload: dict) -> list[SearchResult]:
    web_section = payload.get("web") or {}
    raw_results = web_section.get("results") or []
    results: list[SearchResult] = []
    for item in raw_results:
        title = _clean_text(item.get("title") or "")
        url = item.get("url") or ""
        if not title or not url:
            continue
        snippets = [item.get("description") or ""]
        snippets.extend(item.get("extra_snippets") or [])
        snippet = _clean_text("\n".join(part for part in snippets if part))
        results.append(SearchResult(title=title, url=url, snippet=snippet))
    return _deduplicate_results(results)


def _extract_openai_output_text(response: Any) -> str:
    output_text = _field(response, "output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    text_parts: list[str] = []
    for output_item in _field(response, "output", []) or []:
        if _field(output_item, "type") != "message":
            continue
        for content_item in _field(output_item, "content", []) or []:
            text = _field(content_item, "text")
            if isinstance(text, str):
                text_parts.append(text)
    return "".join(text_parts).strip()


def _extract_openai_citation_results(response: Any) -> list[SearchResult]:
    results: list[SearchResult] = []
    for output_item in _field(response, "output", []) or []:
        if _field(output_item, "type") != "message":
            continue
        for content_item in _field(output_item, "content", []) or []:
            annotations = _field(content_item, "annotations", []) or []
            for annotation in annotations:
                if _field(annotation, "type") != "url_citation":
                    continue
                url = _field(annotation, "url") or ""
                title = _clean_text(_field(annotation, "title") or url)
                if url:
                    results.append(SearchResult(title=title, url=url))
    return _deduplicate_results(results)


def _extract_openai_source_results(response: Any) -> list[SearchResult]:
    results: list[SearchResult] = []
    for output_item in _field(response, "output", []) or []:
        if _field(output_item, "type") != "web_search_call":
            continue
        action = _field(output_item, "action", {}) or {}
        for source in _field(action, "sources", []) or []:
            url = _field(source, "url") or ""
            title = _clean_text(_field(source, "title") or url)
            if url:
                results.append(SearchResult(title=title, url=url))
    return _deduplicate_results(results)


def _build_openai_search_input(
    query: str,
    result_limit: int,
    freshness: Optional[str],
    search_lang: Optional[str],
) -> str:
    hints = [f"Search query: {query}", f"Return up to {result_limit} useful sources."]
    if freshness:
        hints.append(f"Prefer sources with this freshness hint when possible: {freshness}.")
    if search_lang:
        hints.append(f"Prefer sources in this language when possible: {search_lang}.")
    hints.append("Include concise findings and preserve source citations.")
    return "\n".join(hints)


def _decode_duckduckgo_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    parsed_url = urlparse(raw_url)
    query_values = parse_qs(parsed_url.query)
    if "uddg" in query_values and query_values["uddg"]:
        return unquote(query_values["uddg"][0])
    if raw_url.startswith("//"):
        return "https:" + raw_url
    if raw_url.startswith("/"):
        return "https://duckduckgo.com" + raw_url
    return raw_url


def _deduplicate_results(results: list[SearchResult]) -> list[SearchResult]:
    seen_urls: set[str] = set()
    unique_results: list[SearchResult] = []
    for result in results:
        normalized_url = result.url.strip()
        if not normalized_url or normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        unique_results.append(result)
    return unique_results


def _clean_text(text: str) -> str:
    return " ".join(unescape(text).split())


def _normalize_result_limit(max_results: int) -> int:
    try:
        requested = int(max_results)
    except (TypeError, ValueError):
        requested = AgentConfig.DEFAULT_SEARCH_RESULTS
    return max(1, min(requested, AgentConfig.MAX_SEARCH_RESULTS))


def _normalize_country(country: Optional[str]) -> str:
    if not country:
        return ""
    country_code = country.strip().upper()
    return country_code if len(country_code) == 2 else ""


def _normalize_brave_freshness(freshness: Optional[str]) -> str:
    if not freshness:
        return ""
    normalized = freshness.strip().lower()
    return {
        "day": "pd",
        "daily": "pd",
        "24h": "pd",
        "week": "pw",
        "weekly": "pw",
        "month": "pm",
        "monthly": "pm",
        "year": "py",
        "yearly": "py",
    }.get(normalized, normalized)


def _normalize_duckduckgo_freshness(freshness: Optional[str]) -> str:
    if not freshness:
        return ""
    normalized = freshness.strip().lower()
    return {
        "day": "d",
        "daily": "d",
        "24h": "d",
        "pd": "d",
        "week": "w",
        "weekly": "w",
        "pw": "w",
        "month": "m",
        "monthly": "m",
        "pm": "m",
        "year": "y",
        "yearly": "y",
        "py": "y",
    }.get(normalized, normalized)


def _duckduckgo_region(country: Optional[str], search_lang: Optional[str]) -> str:
    normalized_country = _normalize_country(country)
    if not normalized_country or not search_lang:
        return ""
    return f"{normalized_country.lower()}-{search_lang.strip().lower()}"


def _get_brave_api_key(config: dict) -> str:
    configured_key = str(config.get("api_key") or config.get("subscription_token") or "").strip()
    if configured_key:
        return configured_key
    for env_name in BRAVE_API_KEY_ENV_VARS:
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            return env_value
    return ""


def is_placeholder_api_key(api_key: str) -> bool:
    """Return whether an API key is empty or still a template placeholder."""
    normalized = api_key.strip().lower()
    return not normalized or normalized in PLACEHOLDER_API_KEYS


def _is_placeholder_api_key(api_key: str) -> bool:
    return is_placeholder_api_key(api_key)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    value = getattr(obj, name, default)
    if value is not default:
        return value
    model_extra = getattr(obj, "model_extra", None)
    if isinstance(model_extra, dict) and name in model_extra:
        return model_extra.get(name, default)
    return value


def _error_response(provider: str, query: str, message: str) -> dict:
    return {
        "status": "error",
        "provider": provider,
        "query": query,
        "message": message,
        "results": [],
    }
