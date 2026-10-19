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
SEARCH_PROVIDER_DEEPSEEK = "deepseek"
SEARCH_PROVIDER_NONE = "none"
SUPPORTED_SEARCH_PROVIDERS = {
    SEARCH_PROVIDER_OPENAI,
    SEARCH_PROVIDER_DEEPSEEK,
    SEARCH_PROVIDER_NONE,
}

DEEPSEEK_SEARCH_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_API_KEY_ENV_VARS = ("DEEPSEEK_API_KEY", "DEEPSEEK_API_TOKEN")
DEEPSEEK_SEARCH_TIMEOUT = 30.0
DEEPSEEK_MAX_QUERY_LENGTH = 128
PLACEHOLDER_API_KEYS = {
    "your_api_key",
    "your_api_key_here",
    "your_openai_api_key",
    "your_openai_api_key_here",
    "your_deepseek_api_key",
    "your_deepseek_api_key_here",
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
    SEARCH_PROVIDER_DEEPSEEK: {
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
        if self.provider == SEARCH_PROVIDER_DEEPSEEK:
            return await self._search_deepseek(
                query=query,
                result_limit=result_limit,
            )
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

    async def _search_deepseek(
        self,
        *,
        query: str,
        result_limit: int,
    ) -> dict:
        api_key = _get_deepseek_api_key(self.config)
        if not api_key:
            return _error_response(
                self.provider,
                query,
                "DeepSeek search requires search.api_key, provider.api_key, or DEEPSEEK_API_KEY.",
            )

        try:
            normalized_need_content = _normalize_optional_bool(
                self.config.get("need_content"),
                default=False,
            )
        except ValueError as exception:
            return _error_response(self.provider, query, str(exception))

        payload: dict[str, Any] = {
            "query": query[:DEEPSEEK_MAX_QUERY_LENGTH],
            "count": result_limit,
            "need_content": bool(normalized_need_content),
        }
        ori_question = _clean_optional(self.config.get("ori_question"))
        if ori_question:
            payload["ori_question"] = ori_question[:DEEPSEEK_MAX_QUERY_LENGTH]
        localization = _clean_optional(self.config.get("localization"))
        if localization:
            payload["localization"] = localization

        endpoint = f"{_deepseek_search_base_url(self.config)}/v1/tools/search"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(DEEPSEEK_SEARCH_TIMEOUT)) as http_client:
                response = await http_client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if response.status_code >= 400:
                    return _error_response(
                        self.provider,
                        query,
                        _extract_deepseek_error_message(response),
                    )
                data = response.json()
        except Exception as exception:
            logger.warning("DeepSeek web search failed: %s", exception)
            return _error_response(self.provider, query, str(exception))

        raw_results = data.get("results") if isinstance(data, dict) else []
        results: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for item in raw_results or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            title = _clean_text(_first_text_field(item, "title", "site_name") or url)
            snippet = _clean_text(_first_text_field(item, "summary", "snippet") or "")
            entry: dict[str, Any] = {
                "title": title,
                "url": url,
                "snippet": snippet,
            }
            if item.get("date"):
                entry["date"] = str(item["date"]).strip()
            if item.get("site_name"):
                entry["site_name"] = _clean_text(str(item["site_name"]))
            if item.get("rerank_score") is not None:
                entry["rerank_score"] = item["rerank_score"]
            if item.get("authority") is not None:
                entry["authority"] = item["authority"]
            if normalized_need_content and item.get("content"):
                entry["content"] = str(item["content"]).strip()
            results.append(entry)
            if len(results) >= result_limit:
                break

        return {
            "status": "ok" if results else "empty",
            "provider": self.provider,
            "query": query,
            "answer": "",
            "results": results,
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
            "Search current or external web information and return concise source-backed results. "
            "Cite only returned URLs. If results are weak or empty, say so and answer only from reliable context."
        ),
        param_descriptions={
            "query": (
                "Search query with key entities, dates, and constraints. "
                "Supports operators such as site:, -site:, \"exact phrase\", lang:zh,en, after:YYYY-MM-DD, before:YYYY-MM-DD."
            ),
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
        "deepseek_search": SEARCH_PROVIDER_DEEPSEEK,
        "deepseek_web_search": SEARCH_PROVIDER_DEEPSEEK,
        "deepseek_search_api": SEARCH_PROVIDER_DEEPSEEK,
        "deepseek": SEARCH_PROVIDER_DEEPSEEK,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_SEARCH_PROVIDERS:
        allowed = ", ".join(sorted(SUPPORTED_SEARCH_PROVIDERS))
        raise ValueError(f"Unsupported search provider: {provider}. Expected one of: {allowed}")
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


def _build_openai_search_input(query: str, result_limit: int) -> str:
    hints = [f"Search query: {query}", f"Return up to {result_limit} useful sources."]
    hints.append("Include concise findings and preserve source citations.")
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


def _get_deepseek_api_key(config: dict) -> str:
    configured_key = str(config.get("api_key") or "").strip()
    if configured_key and not is_placeholder_api_key(configured_key):
        return configured_key
    for env_name in DEEPSEEK_API_KEY_ENV_VARS:
        env_value = os.getenv(env_name, "").strip()
        if env_value and not is_placeholder_api_key(env_value):
            return env_value
    return ""


def _deepseek_search_base_url(config: dict) -> str:
    raw = str(config.get("base_url") or DEEPSEEK_SEARCH_BASE_URL).strip().rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[:-3].rstrip("/")
    return raw or DEEPSEEK_SEARCH_BASE_URL


def _extract_deepseek_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            message = str(error_obj.get("message") or "").strip()
            if message:
                return f"HTTP {response.status_code}: {message}"
        message = str(payload.get("message") or "").strip()
        if message:
            return f"HTTP {response.status_code}: {message}"
    reason = getattr(response, "reason_phrase", "") or "Request failed"
    return f"HTTP {response.status_code}: {reason}"


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
