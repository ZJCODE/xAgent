"""Configurable web search tool implementations."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from html import unescape
from typing import Any, Literal, Optional

import httpx
from openai import AsyncOpenAI

from xagent.core.config import AgentConfig
from xagent.utils.tool_decorator import function_tool


logger = logging.getLogger(__name__)

SEARCH_PROVIDER_OPENAI = "openai"
SEARCH_PROVIDER_QWEN = "qwen"
SEARCH_PROVIDER_MINIMAX = "minimax"
SEARCH_PROVIDER_NONE = "none"
SUPPORTED_SEARCH_PROVIDERS = {
    SEARCH_PROVIDER_OPENAI,
    SEARCH_PROVIDER_QWEN,
    SEARCH_PROVIDER_MINIMAX,
    SEARCH_PROVIDER_NONE,
}

DEFAULT_QWEN_SEARCH_MODEL = "qwen3-max-2026-01-23"
QWEN_COMPATIBLE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_API_KEY_ENV_VARS = ("DASHSCOPE_API_KEY", "DASHSCOPE_API_TOKEN", "QWEN_API_KEY", "QWEN_API_TOKEN")
MINIMAX_SEARCH_ENDPOINT = "https://api.minimaxi.com/v1/coding_plan/search"
MINIMAX_SEARCH_TIMEOUT = 30.0
MINIMAX_API_KEY_ENV_VARS = ("MINIMAX_API_KEY", "MINIMAX_API_TOKEN")
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
}

OPENAI_SEARCH_CONTEXT_SIZES = {"low", "medium", "high"}
OPENAI_RETURN_TOKEN_BUDGETS = {"default", "unlimited"}
SEARCH_TOOL_PARAMETERS = {
    SEARCH_PROVIDER_OPENAI: {
        "query",
        "max_results",
        "search_context_size",
        "country",
        "city",
        "region",
        "timezone",
        "allowed_domains",
        "blocked_domains",
        "external_web_access",
        "return_token_budget",
        "force_search",
    },
    SEARCH_PROVIDER_QWEN: {
        "query",
        "max_results",
        "enable_thinking",
        "web_extractor",
        "code_interpreter",
    },
    SEARCH_PROVIDER_MINIMAX: {
        "query",
        "max_results",
    },
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
        search_context_size: Optional[Literal["low", "medium", "high"]] = None,
        country: Optional[str] = None,
        city: Optional[str] = None,
        region: Optional[str] = None,
        timezone: Optional[str] = None,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
        external_web_access: Optional[bool] = None,
        return_token_budget: Optional[Literal["default", "unlimited"]] = None,
        force_search: Optional[bool] = None,
        enable_thinking: Optional[bool] = None,
        web_extractor: Optional[bool] = None,
        code_interpreter: Optional[bool] = None,
        freshness: Optional[str] = None,
        search_lang: Optional[str] = None,
    ) -> dict:
        query = query.strip()
        if not query:
            return _error_response(self.provider, query, "query is required")

        result_limit = _normalize_result_limit(max_results)
        provided_parameters = {
            "search_context_size": search_context_size,
            "country": country,
            "city": city,
            "region": region,
            "timezone": timezone,
            "allowed_domains": allowed_domains,
            "blocked_domains": blocked_domains,
            "external_web_access": external_web_access,
            "return_token_budget": return_token_budget,
            "force_search": force_search,
            "enable_thinking": enable_thinking,
            "web_extractor": web_extractor,
            "code_interpreter": code_interpreter,
            "freshness": freshness,
            "search_lang": search_lang,
        }
        unsupported = _unsupported_parameters(self.provider, provided_parameters)
        if unsupported:
            return _error_response(
                self.provider,
                query,
                f"{self.provider} web search does not support parameter(s): {', '.join(unsupported)}",
            )

        if self.provider == SEARCH_PROVIDER_OPENAI:
            return await self._search_openai(
                query=query,
                result_limit=result_limit,
                search_context_size=search_context_size,
                country=country,
                city=city,
                region=region,
                timezone=timezone,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                external_web_access=external_web_access,
                return_token_budget=return_token_budget,
                force_search=force_search,
            )
        if self.provider == SEARCH_PROVIDER_QWEN:
            return await self._search_qwen(
                query=query,
                result_limit=result_limit,
                enable_thinking=enable_thinking,
                web_extractor=web_extractor,
                code_interpreter=code_interpreter,
            )
        if self.provider == SEARCH_PROVIDER_MINIMAX:
            return await self._search_minimax(query=query, result_limit=result_limit)
        return _error_response(self.provider, query, "search is disabled")

    async def _search_openai(
        self,
        *,
        query: str,
        result_limit: int,
        search_context_size: Optional[str],
        country: Optional[str],
        city: Optional[str],
        region: Optional[str],
        timezone: Optional[str],
        allowed_domains: Optional[list[str]],
        blocked_domains: Optional[list[str]],
        external_web_access: Optional[bool],
        return_token_budget: Optional[str],
        force_search: Optional[bool],
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

        try:
            tool_config = _build_openai_tool_config(
                config=self.config,
                search_context_size=search_context_size,
                country=country,
                city=city,
                region=region,
                timezone=timezone,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                external_web_access=external_web_access,
                return_token_budget=return_token_budget,
            )
            normalized_force_search = _normalize_optional_bool(
                _option_value(force_search, self.config, "force_search")
            )
        except ValueError as exception:
            return _error_response(self.provider, query, str(exception))

        try:
            response = await search_client.responses.create(
                model=self.model or self.config.get("model") or AgentConfig.DEFAULT_MODEL,
                tools=[tool_config],
                tool_choice="required" if normalized_force_search else "auto",
                include=["web_search_call.action.sources"],
                input=_build_openai_search_input(query, result_limit),
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

    async def _search_qwen(
        self,
        *,
        query: str,
        result_limit: int,
        enable_thinking: Optional[bool],
        web_extractor: Optional[bool],
        code_interpreter: Optional[bool],
    ) -> dict:
        search_client = self.client
        if search_client is None:
            try:
                search_client = _create_qwen_search_client(self.config)
            except Exception as exception:
                return _error_response(
                    self.provider,
                    query,
                    f"Qwen search client is not configured: {exception}",
                )
        if search_client is None:
            return _error_response(
                self.provider,
                query,
                "Qwen search requires search.api_key, provider.api_key, or DASHSCOPE_API_KEY.",
            )

        try:
            normalized_enable_thinking = _normalize_optional_bool(
                _option_value(enable_thinking, self.config, "enable_thinking"),
                default=True,
            )
            normalized_web_extractor = _normalize_optional_bool(
                _option_value(web_extractor, self.config, "web_extractor"),
                default=True,
            )
            normalized_code_interpreter = _normalize_optional_bool(
                _option_value(code_interpreter, self.config, "code_interpreter"),
                default=True,
            )
        except ValueError as exception:
            return _error_response(self.provider, query, str(exception))

        tools: list[dict[str, Any]] = [{"type": "web_search"}]
        if normalized_web_extractor:
            tools.append({"type": "web_extractor"})
        if normalized_code_interpreter:
            tools.append({"type": "code_interpreter"})

        request: dict[str, Any] = {
            "model": self.model or self.config.get("model") or DEFAULT_QWEN_SEARCH_MODEL,
            "input": _build_qwen_search_input(query, result_limit),
            "tools": tools,
        }
        if normalized_enable_thinking:
            request["extra_body"] = {"enable_thinking": True}

        try:
            response = await search_client.responses.create(**request)
        except Exception as exception:
            logger.warning("Qwen web search failed: %s", exception)
            return _error_response(self.provider, query, str(exception))

        answer = _extract_openai_output_text(response)
        citation_results = _extract_openai_citation_results(response)
        source_results = _extract_openai_source_results(response)
        structured_results = _extract_structured_url_results(response)
        results = _deduplicate_results(citation_results + source_results + structured_results)[:result_limit]
        result = {
            "status": "ok" if answer or results else "empty",
            "provider": self.provider,
            "query": query,
            "answer": answer,
            "results": [item.to_dict() for item in results],
        }
        tool_usage = _extract_tool_usage(response)
        if tool_usage:
            result["tool_usage"] = tool_usage
        return result

    async def _search_minimax(self, *, query: str, result_limit: int) -> dict:
        api_key = _get_minimax_api_key(self.config)
        if not api_key:
            return _error_response(
                self.provider,
                query,
                "MiniMax search requires search.api_key, provider.api_key, or MINIMAX_API_KEY.",
            )

        endpoint = _minimax_search_endpoint(self.config)
        try:
            async with httpx.AsyncClient(timeout=MINIMAX_SEARCH_TIMEOUT) as http_client:
                response = await http_client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"q": query},
                )
                response.raise_for_status()
                response_json = response.json()
        except Exception as exception:
            logger.warning("MiniMax web search failed: %s", exception)
            return _error_response(self.provider, query, str(exception))

        answer = _extract_minimax_answer(response_json)
        results = _extract_minimax_results(response_json)[:result_limit]
        return {
            "status": "ok" if answer or results else "empty",
            "provider": self.provider,
            "query": query,
            "answer": answer,
            "results": [item.to_dict() for item in results],
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
            "Search current or external web information and return concise source-backed results."
        ),
        param_descriptions={
            "query": "Search query with key entities, dates, and constraints.",
            "max_results": "Number of normalized source results, 1-20.",
            "search_context_size": "OpenAI only. Web search context size: low, medium, or high.",
            "country": "OpenAI only. Approximate two-letter country code such as US, CN, GB, or DE.",
            "city": "OpenAI only. Approximate user city for local search results.",
            "region": "OpenAI only. Approximate state, province, or region for local search results.",
            "timezone": "OpenAI only. IANA timezone such as America/Chicago or Asia/Shanghai.",
            "allowed_domains": "OpenAI only. Domains to allow, without http:// or https:// prefixes.",
            "blocked_domains": "OpenAI only. Domains to block, without http:// or https:// prefixes.",
            "external_web_access": "OpenAI only. Set false to use cached/indexed web content without live access.",
            "return_token_budget": "OpenAI only. Returned-token budget for GPT-5+ reasoning search: default or unlimited.",
            "force_search": "OpenAI only. Require the web search tool instead of leaving it to auto tool choice.",
            "enable_thinking": "Qwen only. Enable DashScope thinking mode. Defaults to true.",
            "web_extractor": "Qwen only. Include DashScope web_extractor alongside web_search. Defaults to true.",
            "code_interpreter": "Qwen only. Include DashScope code_interpreter alongside web_search. Defaults to true.",
        },
    )
    async def web_search(
        query: str,
        max_results: int = 5,
        search_context_size: Optional[Literal["low", "medium", "high"]] = None,
        country: Optional[str] = None,
        city: Optional[str] = None,
        region: Optional[str] = None,
        timezone: Optional[str] = None,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
        external_web_access: Optional[bool] = None,
        return_token_budget: Optional[Literal["default", "unlimited"]] = None,
        force_search: Optional[bool] = None,
        enable_thinking: Optional[bool] = None,
        web_extractor: Optional[bool] = None,
        code_interpreter: Optional[bool] = None,
        freshness: Optional[str] = None,
        search_lang: Optional[str] = None,
    ) -> dict:
        return await search_provider.search(
            query=query,
            max_results=max_results,
            search_context_size=search_context_size,
            country=country,
            city=city,
            region=region,
            timezone=timezone,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            external_web_access=external_web_access,
            return_token_budget=return_token_budget,
            force_search=force_search,
            enable_thinking=enable_thinking,
            web_extractor=web_extractor,
            code_interpreter=code_interpreter,
            freshness=freshness,
            search_lang=search_lang,
        )

    _limit_tool_schema_to_provider(web_search.tool_spec, provider)
    return web_search


def _limit_tool_schema_to_provider(tool_spec: dict, provider: str) -> None:
    supported_parameters = SEARCH_TOOL_PARAMETERS.get(provider)
    if not supported_parameters:
        return

    function_spec = tool_spec.get("function") or {}
    parameters = function_spec.get("parameters") or {}
    properties = parameters.get("properties") or {}
    parameters["properties"] = {
        name: schema
        for name, schema in properties.items()
        if name in supported_parameters
    }
    required = parameters.get("required") or []
    parameters["required"] = [name for name in required if name in supported_parameters]


def normalize_search_provider(provider: Any) -> str:
    normalized = str(provider or SEARCH_PROVIDER_NONE).strip().lower().replace("-", "_")
    aliases = {
        "off": SEARCH_PROVIDER_NONE,
        "disabled": SEARCH_PROVIDER_NONE,
        "no_search": SEARCH_PROVIDER_NONE,
        "none": SEARCH_PROVIDER_NONE,
        "openai_builtin": SEARCH_PROVIDER_OPENAI,
        "openai_web_search": SEARCH_PROVIDER_OPENAI,
        "openai": SEARCH_PROVIDER_OPENAI,
        "dashscope": SEARCH_PROVIDER_QWEN,
        "qwen_search": SEARCH_PROVIDER_QWEN,
        "qwen_web_search": SEARCH_PROVIDER_QWEN,
        "qwen": SEARCH_PROVIDER_QWEN,
        "minimax_search": SEARCH_PROVIDER_MINIMAX,
        "minimax_web_search": SEARCH_PROVIDER_MINIMAX,
        "minimax": SEARCH_PROVIDER_MINIMAX,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_SEARCH_PROVIDERS:
        raise ValueError(f"Unsupported search provider: {provider}")
    return normalized


def _unsupported_parameters(provider: str, provided_parameters: dict[str, Any]) -> list[str]:
    supported_parameters = SEARCH_TOOL_PARAMETERS.get(provider, set())
    return [
        name
        for name, value in provided_parameters.items()
        if name not in supported_parameters and _parameter_was_provided(value)
    ]


def _parameter_was_provided(value: Any) -> bool:
    return value not in (None, "", [], {}, ())


def _build_openai_tool_config(
    *,
    config: dict,
    search_context_size: Optional[str],
    country: Optional[str],
    city: Optional[str],
    region: Optional[str],
    timezone: Optional[str],
    allowed_domains: Optional[list[str]],
    blocked_domains: Optional[list[str]],
    external_web_access: Optional[bool],
    return_token_budget: Optional[str],
) -> dict[str, Any]:
    normalized_context_size = _normalize_openai_search_context_size(
        _option_value(search_context_size, config, "search_context_size"),
        default="medium",
    )
    tool_config: dict[str, Any] = {
        "type": "web_search",
        "search_context_size": normalized_context_size,
    }

    location = _openai_user_location(
        country=_option_value(country, config, "country"),
        city=_option_value(city, config, "city"),
        region=_option_value(region, config, "region"),
        timezone=_option_value(timezone, config, "timezone"),
    )
    if location:
        tool_config["user_location"] = location

    normalized_allowed_domains = _normalize_domain_list(
        _option_value(allowed_domains, config, "allowed_domains"),
        field_name="allowed_domains",
    )
    normalized_blocked_domains = _normalize_domain_list(
        _option_value(blocked_domains, config, "blocked_domains"),
        field_name="blocked_domains",
    )
    if normalized_allowed_domains and normalized_blocked_domains:
        raise ValueError("OpenAI web search supports allowed_domains or blocked_domains, not both")
    if normalized_allowed_domains:
        tool_config["filters"] = {"allowed_domains": normalized_allowed_domains}
    if normalized_blocked_domains:
        tool_config["filters"] = {"blocked_domains": normalized_blocked_domains}

    normalized_external_access = _normalize_optional_bool(
        _option_value(external_web_access, config, "external_web_access")
    )
    if normalized_external_access is not None:
        tool_config["external_web_access"] = normalized_external_access

    normalized_token_budget = _normalize_openai_return_token_budget(
        _option_value(return_token_budget, config, "return_token_budget")
    )
    if normalized_token_budget:
        tool_config["return_token_budget"] = normalized_token_budget

    return tool_config


def _openai_user_location(
    *,
    country: Any,
    city: Any,
    region: Any,
    timezone: Any,
) -> dict[str, str]:
    location: dict[str, str] = {"type": "approximate"}
    normalized_country = _normalize_country(country)
    if country and not normalized_country:
        raise ValueError("country must be a two-letter country code")
    if normalized_country:
        location["country"] = normalized_country
    for key, value in (("city", city), ("region", region), ("timezone", timezone)):
        normalized_value = _clean_optional(value)
        if normalized_value:
            location[key] = normalized_value
    return location if len(location) > 1 else {}


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


def _extract_structured_url_results(response: Any) -> list[SearchResult]:
    results: list[SearchResult] = []
    for output_item in _field(response, "output", []) or []:
        results.extend(_extract_structured_url_results_from_value(output_item))
    return _deduplicate_results(results)


def _extract_structured_url_results_from_value(value: Any) -> list[SearchResult]:
    if isinstance(value, list):
        results: list[SearchResult] = []
        for item in value:
            results.extend(_extract_structured_url_results_from_value(item))
        return results

    data = _as_mapping(value)
    if not data:
        return []

    results = []
    url = _first_text_field(data, "url", "link", "source_url")
    if url:
        title = _clean_text(_first_text_field(data, "title", "name", "site_name") or url)
        snippet = _clean_text(_first_text_field(data, "snippet", "description", "summary") or "")
        results.append(SearchResult(title=title, url=url, snippet=snippet))

    for key in ("sources", "results", "citations", "references", "items"):
        nested = data.get(key)
        if nested is not None:
            results.extend(_extract_structured_url_results_from_value(nested))
    return results


def _extract_tool_usage(response: Any) -> dict[str, int]:
    usage = _field(response, "usage", {}) or {}
    x_tools = _field(usage, "x_tools", {}) or {}
    if not isinstance(x_tools, dict):
        return {}

    tool_usage: dict[str, int] = {}
    for tool_name, details in x_tools.items():
        count = _field(details, "count", 0)
        try:
            normalized_count = int(count)
        except (TypeError, ValueError):
            continue
        tool_usage[str(tool_name)] = normalized_count
    return tool_usage


def _extract_minimax_answer(response: Any) -> str:
    data = _as_mapping(response)
    for candidate in (
        data,
        _as_mapping(data.get("data")),
        _as_mapping(data.get("result")),
    ):
        answer = _first_text_field(candidate, "answer", "summary", "text", "content")
        if answer:
            return _clean_text(answer)
    return ""


def _extract_minimax_results(response: Any) -> list[SearchResult]:
    results = _extract_minimax_results_from_value(response)
    return _deduplicate_results(results)


def _extract_minimax_results_from_value(value: Any) -> list[SearchResult]:
    if isinstance(value, list):
        results: list[SearchResult] = []
        for item in value:
            results.extend(_extract_minimax_results_from_value(item))
        return results

    data = _as_mapping(value)
    if not data:
        return []

    results: list[SearchResult] = []
    url = _first_text_field(data, "url", "link", "source_url", "source")
    if url:
        title = _clean_text(_first_text_field(data, "title", "name", "site_name") or url)
        snippet = _clean_text(
            _first_text_field(data, "snippet", "description", "summary", "content", "text") or ""
        )
        results.append(SearchResult(title=title, url=url, snippet=snippet))

    for key in (
        "organic",
        "data",
        "result",
        "results",
        "items",
        "sources",
        "citations",
        "references",
        "webpages",
    ):
        nested = data.get(key)
        if nested is not None:
            results.extend(_extract_minimax_results_from_value(nested))
    return results


def _build_openai_search_input(query: str, result_limit: int) -> str:
    hints = [f"Search query: {query}", f"Return up to {result_limit} useful sources."]
    hints.append("Include concise findings and preserve source citations.")
    return "\n".join(hints)


def _build_qwen_search_input(query: str, result_limit: int) -> str:
    hints = [query, "", f"Use web search and extraction when useful. Return up to {result_limit} useful sources."]
    hints.append("Provide a concise answer and keep source URLs available in the response.")
    return "\n".join(hints)


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


def _clean_optional(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_result_limit(max_results: int) -> int:
    try:
        requested = int(max_results)
    except (TypeError, ValueError):
        requested = AgentConfig.DEFAULT_SEARCH_RESULTS
    return max(1, min(requested, AgentConfig.MAX_SEARCH_RESULTS))


def _normalize_country(country: Any) -> str:
    if not country:
        return ""
    country_code = str(country).strip().upper()
    return country_code if len(country_code) == 2 else ""


def _normalize_openai_search_context_size(value: Any, *, default: str) -> str:
    normalized = _clean_optional(value).lower() or default
    if normalized not in OPENAI_SEARCH_CONTEXT_SIZES:
        allowed = ", ".join(sorted(OPENAI_SEARCH_CONTEXT_SIZES))
        raise ValueError(f"search_context_size must be one of: {allowed}")
    return normalized


def _normalize_openai_return_token_budget(value: Any) -> str:
    normalized = _clean_optional(value).lower()
    if not normalized:
        return ""
    if normalized not in OPENAI_RETURN_TOKEN_BUDGETS:
        allowed = ", ".join(sorted(OPENAI_RETURN_TOKEN_BUDGETS))
        raise ValueError(f"return_token_budget must be one of: {allowed}")
    return normalized


def _normalize_domain_list(value: Any, *, field_name: str) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        raw_domains = [part for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw_domains = list(value)
    else:
        raise ValueError(f"{field_name} must be a list of domains")

    domains: list[str] = []
    for raw_domain in raw_domains:
        domain = str(raw_domain or "").strip().lower()
        if not domain:
            continue
        for prefix in ("https://", "http://"):
            if domain.startswith(prefix):
                domain = domain[len(prefix):]
        domain = domain.split("/", 1)[0]
        if domain and domain not in domains:
            domains.append(domain)
    if len(domains) > 100:
        raise ValueError(f"{field_name} can include at most 100 domains")
    return domains


def _normalize_optional_bool(value: Any, *, default: Optional[bool] = None) -> Optional[bool]:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Expected a boolean value, got: {value}")


def _option_value(call_value: Any, config: dict, key: str) -> Any:
    return call_value if call_value is not None else config.get(key)


def _get_qwen_api_key(config: dict) -> str:
    configured_key = str(config.get("api_key") or "").strip()
    if configured_key and not is_placeholder_api_key(configured_key):
        return configured_key
    for env_name in QWEN_API_KEY_ENV_VARS:
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            return env_value
    return ""


def _qwen_search_base_url(config: dict) -> str:
    return str(config.get("base_url") or QWEN_COMPATIBLE_BASE_URL).strip().rstrip("/")


def _create_qwen_search_client(config: dict) -> Optional[AsyncOpenAI]:
    api_key = _get_qwen_api_key(config)
    if not api_key:
        return None
    return AsyncOpenAI(api_key=api_key, base_url=_qwen_search_base_url(config))


def _get_minimax_api_key(config: dict) -> str:
    configured_key = str(config.get("api_key") or "").strip()
    if configured_key and not is_placeholder_api_key(configured_key):
        return configured_key
    for env_name in MINIMAX_API_KEY_ENV_VARS:
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            return env_value
    return ""


def _minimax_search_endpoint(config: dict) -> str:
    return str(config.get("endpoint") or MINIMAX_SEARCH_ENDPOINT).strip()


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


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception:
            dumped = None
        if isinstance(dumped, dict):
            return dumped
    model_extra = getattr(value, "model_extra", None)
    if isinstance(model_extra, dict):
        return model_extra
    return {}


def _first_text_field(data: dict[str, Any], *names: str) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _error_response(provider: str, query: str, message: str) -> dict:
    return {
        "status": "error",
        "provider": provider,
        "query": query,
        "message": message,
        "results": [],
    }
