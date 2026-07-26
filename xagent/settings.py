from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .interfaces.voice.config import VoiceChannelConfig


def write_text_atomic(path: str | Path, content: str) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, target)


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_history: int = Field(default=32, gt=0)
    max_iter: int = Field(default=50, gt=0)
    subconscious_activity: float = Field(default=0.0, ge=0.0, le=1.0)
    memory_recent_days: int = Field(default=2, ge=0)


class ShellSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class ToolSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shell: ShellSettings = Field(default_factory=ShellSettings)


class ApiChannelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8010, ge=1, le=65535)


class FeishuChannelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    domain: str | None = None
    log_level: str = "info"
    stream: bool = False
    group_fetch_limit: int = Field(default=10, ge=0, le=100)
    group_fetch_timeout: float = Field(default=5.0, gt=0, le=60)
    group_reply_only_when_mentioned: bool = False


class WeixinChannelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    account_id: str = ""
    owner_user_id: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    bot_type: str = "3"
    channel_version: str = "1.0.0"
    owner_only: bool = True
    allow_users: list[str] = Field(default_factory=list)
    send_typing: bool = True
    typing_keepalive_seconds: float = Field(default=5.0, gt=0)
    typing_ticket_ttl_seconds: float = Field(default=3600.0, gt=0)
    text_max_chars: int = Field(default=2000, gt=0)
    send_chunk_delay_seconds: float = Field(default=0.8, ge=0)
    send_retries: int = Field(default=2, ge=0)
    send_retry_delay_seconds: float = Field(default=1.0, gt=0)
    poll_timeout_ms: int = Field(default=35_000, gt=0)
    api_timeout_ms: int = Field(default=15_000, gt=0)
    qr_timeout_seconds: int = Field(default=300, gt=0)
    retry_delay_seconds: float = Field(default=2.0, gt=0)
    backoff_delay_seconds: float = Field(default=30.0, gt=0)
    max_consecutive_failures: int = Field(default=3, gt=0)
    media_enabled: bool = True


class ChannelsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api: ApiChannelSettings = Field(default_factory=ApiChannelSettings)
    feishu: FeishuChannelSettings = Field(default_factory=FeishuChannelSettings)
    weixin: WeixinChannelSettings = Field(default_factory=WeixinChannelSettings)
    voice: VoiceChannelConfig = Field(
        default_factory=lambda: VoiceChannelConfig(enabled=False)
    )


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heartbeat_enabled: bool = True
    heartbeat_interval_seconds: float = Field(default=300.0, gt=0)
    turn_timeout_seconds: float = Field(default=600.0, gt=0, le=600.0)
    tool_timeout_seconds: float = Field(default=300.0, gt=0, le=300.0)


class ReasoningSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    effort: str | None = None
    budget_tokens: int | None = Field(default=None, gt=0)


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["openai", "deepseek", "minimax", "qwen", "anthropic", "custom"]
    model: str = Field(min_length=1)
    api_key: str = ""
    base_url: str = ""
    model_api: Literal[
        "openai_responses",
        "openai_chat_completions",
        "anthropic_messages",
    ] | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    reasoning: ReasoningSettings | None = None
    supports_vision: bool | None = None

    @model_validator(mode="after")
    def validate_protocol(self) -> "ProviderSettings":
        if self.name == "custom" and self.model_api is None:
            raise ValueError("provider.model_api is required for a custom provider")
        if self.name != "custom" and self.model_api is not None:
            raise ValueError("provider.model_api is only valid for a custom provider")
        if self.name != "custom" and self.supports_vision is not None:
            raise ValueError("provider.supports_vision is only valid for a custom provider")
        return self


class SearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["none", "openai", "qwen", "minimax"] = "none"
    api_key: str = ""
    base_url: str = ""
    endpoint: str = ""
    model: str = ""


class ImageGenerationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["none", "openai", "qwen", "minimax"] = "none"
    api_key: str = ""
    base_url: str = ""
    endpoint: str = ""
    model: str = ""
    size: str | None = None
    quality: str | None = None
    output_format: str | None = None
    background: str | None = None
    output_compression: int | None = None
    moderation: str | None = None
    negative_prompt: str | None = None
    prompt_extend: bool | None = None
    watermark: bool | None = None
    aspect_ratio: str | None = None
    width: int | None = None
    height: int | None = None
    n: int | None = None
    seed: int | None = None
    prompt_optimizer: bool | None = None
    aigc_watermark: bool | None = None
    reference_image_url: str | None = None
    reference_image_urls: list[str] | None = None
    subject_reference: list[dict[str, Any]] | None = None
    style: dict[str, Any] | None = None


class ObservabilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: Literal["langfuse"] = "langfuse"
    public_key: str = ""
    secret_key: str = ""
    base_url: str = ""
    sample_rate: float = Field(default=1.0, ge=0, le=1)
    debug: bool = False
    tracing_enabled: bool = True


class XAgentSettings(BaseModel):
    """The only top-level configuration schema for the clean runtime."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    provider: ProviderSettings
    agent: AgentSettings = Field(default_factory=AgentSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    channels: ChannelsSettings = Field(default_factory=ChannelsSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    image_generation: ImageGenerationSettings = Field(default_factory=ImageGenerationSettings)
    observability: ObservabilitySettings | None = None

    @model_validator(mode="after")
    def validate_channel_configuration(self) -> "XAgentSettings":
        if self.channels.feishu.enabled:
            if not self.channels.feishu.app_id or not self.channels.feishu.app_secret:
                raise ValueError("enabled Feishu channel requires app_id and app_secret")
        if self.channels.weixin.enabled and not self.channels.weixin.account_id:
            raise ValueError("enabled Weixin channel requires account_id")
        return self

    @classmethod
    def load(cls, path: str | Path) -> "XAgentSettings":
        config_path = Path(path).expanduser().resolve()
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        return cls.model_validate(raw)

    def write_atomic(self, path: str | Path) -> None:
        config_path = Path(path).expanduser().resolve()
        payload = yaml.safe_dump(
            self.model_dump(
                mode="python",
                exclude_none=True,
                exclude_defaults=True,
            ),
            sort_keys=False,
            allow_unicode=False,
        )
        write_text_atomic(config_path, payload)

    def with_channel_enabled(self, name: str, enabled: bool) -> "XAgentSettings":
        if name not in {"api", "feishu", "weixin", "voice"}:
            raise ValueError(f"unknown channel: {name}")
        data = self.model_dump(mode="python", exclude_none=True)
        data["channels"][name]["enabled"] = bool(enabled)
        return type(self).model_validate(data)

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return cls.model_json_schema()
