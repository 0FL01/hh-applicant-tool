from __future__ import annotations

from dataclasses import dataclass
from os import getenv
from pathlib import Path
from typing import Any

from hh_applicant_tool.context import HHProfileContext
from hh_applicant_tool.services import VacancyResearchService
from hh_applicant_tool.utils import json
from hh_llm_agent.config import (
    DEFAULT_OPENROUTER_BASE_URL,
    DEFAULT_OPENROUTER_MODEL,
    OpenRouterConfig,
)
from hh_llm_agent.openrouter import OpenRouterChatClient


@dataclass(frozen=True)
class MCPServerConfig:
    transport: str = "stdio"
    allow_apply: bool = False
    max_applications_per_run: int = 5
    max_applications_per_day: int = 20
    request_timeout_seconds: float = 20.0
    policy_file: Path | None = None
    default_policy: dict[str, Any] | None = None
    log_level: str = "INFO"


@dataclass
class MCPRuntime:
    profile: HHProfileContext
    service: VacancyResearchService
    config: MCPServerConfig

    @property
    def profile_id(self) -> str:
        return self.profile.profile_id or "."

    def flush_auth_state(self) -> None:
        if "api_client" in self.profile.__dict__:
            self.profile.save_token()
        if "session" in self.profile.__dict__:
            self.profile.save_cookies()


def create_runtime(
    *,
    profile: HHProfileContext,
    config: MCPServerConfig,
) -> MCPRuntime:
    profile.api_client.timeout = config.request_timeout_seconds
    llm_client = _build_llm_client(profile)
    return MCPRuntime(
        profile=profile,
        service=VacancyResearchService(profile, llm_client=llm_client),
        config=config,
    )


def load_server_config(
    profile: HHProfileContext,
    *,
    transport: str = "stdio",
    allow_apply: bool | None = None,
    max_applications_per_run: int | None = None,
    max_applications_per_day: int | None = None,
    request_timeout_seconds: float | None = None,
    policy_file: Path | None = None,
    log_level: str = "INFO",
) -> MCPServerConfig:
    mcp_cfg = profile.config.get("mcp", {})
    policy = dict(profile.config.get("vacancy_policy", {}) or {})
    resolved_policy_file = policy_file or _resolve_policy_file(
        profile,
        mcp_cfg.get("policy_path"),
    )
    if resolved_policy_file is not None and resolved_policy_file.exists():
        with resolved_policy_file.open("r", encoding="utf-8") as fp:
            policy.update(json.load(fp) or {})

    return MCPServerConfig(
        transport=transport or mcp_cfg.get("transport", "stdio"),
        allow_apply=(
            bool(allow_apply)
            if allow_apply is not None
            else bool(mcp_cfg.get("allow_apply", False))
        ),
        max_applications_per_run=(
            max_applications_per_run
            if max_applications_per_run is not None
            else int(mcp_cfg.get("max_applications_per_run", 5))
        ),
        max_applications_per_day=(
            max_applications_per_day
            if max_applications_per_day is not None
            else int(mcp_cfg.get("max_applications_per_day", 20))
        ),
        request_timeout_seconds=(
            request_timeout_seconds
            if request_timeout_seconds is not None
            else float(mcp_cfg.get("request_timeout_seconds", 20.0))
        ),
        policy_file=resolved_policy_file,
        default_policy=policy,
        log_level=log_level,
    )


def _resolve_policy_file(
    profile: HHProfileContext,
    policy_path: str | None,
) -> Path | None:
    if not policy_path:
        return None
    path = Path(policy_path)
    if path.is_absolute():
        return path
    return profile.config_path / path


def _build_llm_client(profile: HHProfileContext) -> OpenRouterChatClient | None:
    app_config = profile.config
    openrouter_cfg = app_config.get("openrouter", {})
    api_key = (
        openrouter_cfg.get("api_key")
        or openrouter_cfg.get("token")
        or app_config.get("api_key")
        or getenv("OPENROUTER_API_KEY")
        or getenv("OPENAI_API_KEY")
    )
    if not api_key:
        return None

    config = OpenRouterConfig(
        api_key=api_key,
        base_url=(
            openrouter_cfg.get("base_url")
            or app_config.get("openai_base_url")
            or getenv("OPENROUTER_BASE_URL")
            or getenv("OPENAI_BASE_URL")
            or DEFAULT_OPENROUTER_BASE_URL
        ),
        model=(
            openrouter_cfg.get("model")
            or getenv("OPENROUTER_MODEL")
            or DEFAULT_OPENROUTER_MODEL
        ),
        temperature=float(openrouter_cfg.get("temperature", 0.2)),
        max_completion_tokens=int(
            openrouter_cfg.get("max_completion_tokens", 1200)
        ),
        request_interval_seconds=float(
            openrouter_cfg.get("request_interval_seconds", 0.35)
        ),
        max_retries_on_rate_limit=int(
            openrouter_cfg.get("max_retries_on_rate_limit", 2)
        ),
        rate_limit_retry_base_seconds=float(
            openrouter_cfg.get("rate_limit_retry_base_seconds", 2.0)
        ),
        reasoning_enabled=bool(openrouter_cfg.get("reasoning_enabled", True)),
        app_name=openrouter_cfg.get("app_name") or "hh-applicant-tool",
        referer=(
            openrouter_cfg.get("referer")
            or "https://github.com/s3rgeym/hh-applicant-tool"
        ),
        proxies=profile._get_openai_proxies(),
    )
    return OpenRouterChatClient(config)
