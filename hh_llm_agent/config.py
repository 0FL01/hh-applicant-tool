from __future__ import annotations

from dataclasses import dataclass
from os import getenv
from typing import Any

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "google/gemini-3.1-flash-lite-preview"
DEFAULT_SYSTEM_PROMPT = (
    "Ты соискатель на HeadHunter. Отвечай работодателю по-русски, вежливо, "
    "кратко и по делу. Не выдумывай факты о кандидате. Если в переписке не "
    "хватает данных, предложи уточнить детали. Не используй markdown, списки, "
    "эмодзи и канцелярит."
)
DEFAULT_REPLY_INSTRUCTION = (
    "Проанализируй переписку и верни JSON с полями action, reply_text, reason. "
    "action может быть только reply или skip. Если нужен ответ работодателю, "
    "выбери reply и напиши короткий профессиональный текст в reply_text. Если "
    "отвечать не нужно, выбери skip и кратко объясни why в reason."
)


@dataclass(frozen=True)
class OpenRouterConfig:
    api_key: str
    base_url: str = DEFAULT_OPENROUTER_BASE_URL
    model: str = DEFAULT_OPENROUTER_MODEL
    temperature: float = 0.2
    max_completion_tokens: int = 1200
    reasoning_enabled: bool = True
    app_name: str = "hh-applicant-tool"
    referer: str = "https://github.com/s3rgeym/hh-applicant-tool"


@dataclass(frozen=True)
class AgentConfig:
    openrouter: OpenRouterConfig
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    reply_instruction: str = DEFAULT_REPLY_INSTRUCTION
    max_history_messages: int = 12
    period_days: int | None = None
    only_invitations: bool = False
    dry_run: bool = False
    limit: int | None = None
    resume_id: str | None = None
    skip_blacklisted: bool = True
    force: bool = False


def _get_nested(data: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def load_agent_config(tool: Any, args: Any) -> AgentConfig:
    config = tool.config

    openrouter_cfg = config.get("openrouter", {})
    agent_cfg = config.get("chat_agent", {})

    api_key = (
        getattr(args, "openrouter_api_key", None)
        or openrouter_cfg.get("api_key")
        or openrouter_cfg.get("token")
        or getenv("OPENROUTER_API_KEY")
    )
    if not api_key:
        raise ValueError(
            "OpenRouter API key is not configured. Use openrouter.api_key or "
            "OPENROUTER_API_KEY."
        )

    temperature_str = getenv("CHAT_AGENT_TEMPERATURE")
    temperature = (
        float(temperature_str)
        if temperature_str is not None
        else (
            getattr(args, "temperature", None)
            if getattr(args, "temperature", None) is not None
            else openrouter_cfg.get("temperature", 0.2)
        )
    )

    max_tokens_str = getenv("CHAT_AGENT_MAX_COMPLETION_TOKENS")
    max_completion_tokens = (
        int(max_tokens_str)
        if max_tokens_str is not None
        else (
            getattr(args, "max_completion_tokens", None)
            or openrouter_cfg.get("max_completion_tokens", 1200)
        )
    )

    model = (
        getattr(args, "model", None)
        or openrouter_cfg.get("model")
        or getenv("OPENROUTER_MODEL")
        or DEFAULT_OPENROUTER_MODEL
    )

    base_url = (
        getattr(args, "openrouter_base_url", None)
        or openrouter_cfg.get("base_url")
        or getenv("OPENROUTER_BASE_URL")
        or DEFAULT_OPENROUTER_BASE_URL
    )

    period_days_str = getenv("CHAT_AGENT_PERIOD_DAYS")
    period_days = (
        int(period_days_str)
        if period_days_str is not None
        else (
            getattr(args, "period", None)
            if getattr(args, "period", None) is not None
            else agent_cfg.get("period_days")
        )
    )

    max_history_str = getenv("CHAT_AGENT_MAX_HISTORY_MESSAGES")
    max_history_messages = (
        int(max_history_str)
        if max_history_str is not None
        else (
            getattr(args, "max_history_messages", None)
            or agent_cfg.get("max_history_messages", 12)
        )
    )

    system_prompt = (
        getattr(args, "system_prompt", None)
        or agent_cfg.get("system_prompt")
        or getenv("CHAT_AGENT_SYSTEM_PROMPT")
        or DEFAULT_SYSTEM_PROMPT
    )

    reply_instruction = (
        getattr(args, "reply_instruction", None)
        or agent_cfg.get("reply_instruction")
        or getenv("CHAT_AGENT_REPLY_INSTRUCTION")
        or DEFAULT_REPLY_INSTRUCTION
    )

    openrouter = OpenRouterConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        max_completion_tokens=max_completion_tokens,
        reasoning_enabled=getattr(args, "reasoning", None)
        if getattr(args, "reasoning", None) is not None
        else openrouter_cfg.get("reasoning_enabled", True),
        app_name=openrouter_cfg.get("app_name") or "hh-applicant-tool",
        referer=(
            openrouter_cfg.get("referer")
            or "https://github.com/s3rgeym/hh-applicant-tool"
        ),
    )

    return AgentConfig(
        openrouter=openrouter,
        system_prompt=system_prompt,
        reply_instruction=reply_instruction,
        max_history_messages=max_history_messages,
        period_days=period_days,
        only_invitations=getattr(args, "only_invitations", None)
        if getattr(args, "only_invitations", None) is not None
        else agent_cfg.get("only_invitations", False),
        dry_run=getattr(args, "dry_run", False),
        limit=getattr(args, "limit", None) or agent_cfg.get("limit"),
        resume_id=getattr(args, "resume_id", None)
        or agent_cfg.get("resume_id"),
        skip_blacklisted=getattr(args, "skip_blacklisted", None)
        if getattr(args, "skip_blacklisted", None) is not None
        else agent_cfg.get("skip_blacklisted", True),
        force=getattr(args, "force", False),
    )
