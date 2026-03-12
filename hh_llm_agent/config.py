from __future__ import annotations

from dataclasses import dataclass
from os import getenv
from typing import Any

from .timing import AgentTimingConfig

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "google/gemini-3.1-flash-lite-preview"
DEFAULT_CLASSIFIER_MODEL = "google/gemma-3-27b-it"
DEFAULT_SYSTEM_PROMPT = (
    "Ты соискатель на HeadHunter. Отвечай работодателю по-русски, вежливо, "
    "кратко и по делу. Не выдумывай факты о кандидате. Если в переписке не "
    "хватает данных, предложи уточнить детали. Не используй markdown, списки, "
    "эмодзи и канцелярит."
)
DEFAULT_CLASSIFIER_SYSTEM_PROMPT = (
    "Ты классификатор входящих сообщений в чатах hh.ru. Определи, нужно ли "
    "кандидату отвечать прямо сейчас."
)
DEFAULT_REPLY_INSTRUCTION = (
    "Проанализируй переписку и реши, нужно ли отвечать работодателю сейчас. "
    "action может быть только reply или skip. reply_mode может быть single "
    "или qa_series. Если нужен один цельный ответ работодателю, выбери single "
    "и сформулируй reply_text. Если во входящем пакете несколько screening-"
    "вопросов и естественнее ответить короткой серией, выбери qa_series и "
    "сформулируй 2-3 коротких сообщения. Если отвечать не нужно, выбери skip "
    "и кратко объясни причину в reason."
)
DEFAULT_CLASSIFIER_INSTRUCTION = (
    "Проанализируй переписку. action может быть только reply или skip. "
    "category может быть только human_actionable, bot_actionable, "
    "passive_update, marketing_broadcast, system_event или irrelevant. "
    "reply ставь только если в последнем неотвеченном пакете есть прямой "
    "вопрос, screening, просьба подтвердить интерес, сообщить данные или "
    "выполнить следующий шаг. skip ставь для автоуведомлений, брендовых "
    "рассылок, thank-you сообщений без действия, системных событий и прочего "
    "шума. confidence - число от 0 до 1."
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
class ClassifierConfig:
    openrouter: OpenRouterConfig
    enabled: bool = True
    system_prompt: str = DEFAULT_CLASSIFIER_SYSTEM_PROMPT
    instruction: str = DEFAULT_CLASSIFIER_INSTRUCTION
    max_history_messages: int = 8


@dataclass(frozen=True)
class AgentConfig:
    openrouter: OpenRouterConfig
    classifier: ClassifierConfig | None = None
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
    timing: AgentTimingConfig = AgentTimingConfig()


def _parse_env_bool(name: str) -> bool | None:
    value = getenv(name)
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def _get_nested(data: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _env_or_value(
    env_name: str,
    arg_value: Any,
    config_value: Any,
    default: Any,
    caster,
):
    env_value = getenv(env_name)
    if env_value is not None:
        return caster(env_value)
    if arg_value is not None:
        return caster(arg_value)
    if config_value is not None:
        return caster(config_value)
    return default


def load_agent_config(tool: Any, args: Any) -> AgentConfig:
    config = tool.config

    openrouter_cfg = config.get("openrouter", {})
    agent_cfg = config.get("chat_agent", {})
    classifier_cfg = _get_nested(agent_cfg, "classifier", {}) or {}

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
    dry_run = _parse_env_bool("CHAT_AGENT_DRY_RUN")
    if dry_run is None:
        dry_run = _parse_env_bool("HH_AGENT_DRY_RUN")

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

    classifier_enabled = _parse_env_bool("CHAT_AGENT_CLASSIFIER_ENABLED")
    if classifier_enabled is None:
        classifier_enabled = (
            getattr(args, "classifier_enabled", None)
            if getattr(args, "classifier_enabled", None) is not None
            else classifier_cfg.get("enabled", True)
        )

    classifier_temperature_str = getenv("CHAT_AGENT_CLASSIFIER_TEMPERATURE")
    classifier_temperature = (
        float(classifier_temperature_str)
        if classifier_temperature_str is not None
        else (
            getattr(args, "classifier_temperature", None)
            if getattr(args, "classifier_temperature", None) is not None
            else classifier_cfg.get("temperature", 0.0)
        )
    )

    classifier_max_tokens_str = getenv(
        "CHAT_AGENT_CLASSIFIER_MAX_COMPLETION_TOKENS"
    )
    classifier_max_completion_tokens = (
        int(classifier_max_tokens_str)
        if classifier_max_tokens_str is not None
        else (
            getattr(args, "classifier_max_completion_tokens", None)
            or classifier_cfg.get("max_completion_tokens", 256)
        )
    )

    classifier_model = (
        getenv("CHAT_AGENT_CLASSIFIER_MODEL")
        or getattr(args, "classifier_model", None)
        or classifier_cfg.get("model")
        or DEFAULT_CLASSIFIER_MODEL
    )

    classifier_max_history_messages = _env_or_value(
        "CHAT_AGENT_CLASSIFIER_MAX_HISTORY_MESSAGES",
        getattr(args, "classifier_max_history_messages", None),
        classifier_cfg.get("max_history_messages"),
        8,
        int,
    )

    classifier_system_prompt = (
        getenv("CHAT_AGENT_CLASSIFIER_SYSTEM_PROMPT")
        or getattr(args, "classifier_system_prompt", None)
        or classifier_cfg.get("system_prompt")
        or DEFAULT_CLASSIFIER_SYSTEM_PROMPT
    )

    classifier_instruction = (
        getenv("CHAT_AGENT_CLASSIFIER_INSTRUCTION")
        or getattr(args, "classifier_instruction", None)
        or classifier_cfg.get("instruction")
        or DEFAULT_CLASSIFIER_INSTRUCTION
    )

    classifier_reasoning = _parse_env_bool("CHAT_AGENT_CLASSIFIER_REASONING")
    if classifier_reasoning is None:
        classifier_reasoning = (
            getattr(args, "classifier_reasoning", None)
            if getattr(args, "classifier_reasoning", None) is not None
            else classifier_cfg.get("reasoning_enabled", False)
        )

    classifier = ClassifierConfig(
        enabled=bool(classifier_enabled),
        openrouter=OpenRouterConfig(
            api_key=api_key,
            base_url=base_url,
            model=classifier_model,
            temperature=classifier_temperature,
            max_completion_tokens=classifier_max_completion_tokens,
            reasoning_enabled=bool(classifier_reasoning),
            app_name=openrouter.app_name,
            referer=openrouter.referer,
        ),
        system_prompt=classifier_system_prompt,
        instruction=classifier_instruction,
        max_history_messages=classifier_max_history_messages,
    )

    quiet_hours_enabled = _parse_env_bool("CHAT_AGENT_QUIET_HOURS")
    if quiet_hours_enabled is None:
        quiet_hours_enabled = (
            getattr(args, "quiet_hours", None)
            if getattr(args, "quiet_hours", None) is not None
            else agent_cfg.get("quiet_hours_enabled", True)
        )

    timing = AgentTimingConfig(
        sleep_min_minutes=_env_or_value(
            "CHAT_AGENT_SLEEP_MIN_MINUTES",
            getattr(args, "sleep_min_minutes", None),
            agent_cfg.get("sleep_min_minutes"),
            20,
            int,
        ),
        sleep_max_minutes=_env_or_value(
            "CHAT_AGENT_SLEEP_MAX_MINUTES",
            getattr(args, "sleep_max_minutes", None),
            agent_cfg.get("sleep_max_minutes"),
            30,
            int,
        ),
        timezone=_env_or_value(
            "CHAT_AGENT_TIMEZONE",
            getattr(args, "timezone", None),
            agent_cfg.get("timezone"),
            "Europe/Moscow",
            str,
        ),
        quiet_hours_enabled=bool(quiet_hours_enabled),
        quiet_hours_start=_env_or_value(
            "CHAT_AGENT_QUIET_HOURS_START",
            getattr(args, "quiet_hours_start", None),
            agent_cfg.get("quiet_hours_start"),
            "23:00",
            str,
        ),
        quiet_hours_end=_env_or_value(
            "CHAT_AGENT_QUIET_HOURS_END",
            getattr(args, "quiet_hours_end", None),
            agent_cfg.get("quiet_hours_end"),
            "08:00",
            str,
        ),
        wake_jitter_seconds=_env_or_value(
            "CHAT_AGENT_WAKE_JITTER_SECONDS",
            getattr(args, "wake_jitter_seconds", None),
            agent_cfg.get("wake_jitter_seconds"),
            300,
            int,
        ),
        incoming_collect_seconds=_env_or_value(
            "CHAT_AGENT_INCOMING_COLLECT_SECONDS",
            getattr(args, "incoming_collect_seconds", None),
            agent_cfg.get("incoming_collect_seconds"),
            120,
            int,
        ),
        reply_delay_min_seconds=_env_or_value(
            "CHAT_AGENT_REPLY_DELAY_MIN_SECONDS",
            getattr(args, "reply_delay_min_seconds", None),
            agent_cfg.get("reply_delay_min_seconds"),
            15,
            int,
        ),
        reply_delay_max_seconds=_env_or_value(
            "CHAT_AGENT_REPLY_DELAY_MAX_SECONDS",
            getattr(args, "reply_delay_max_seconds", None),
            agent_cfg.get("reply_delay_max_seconds"),
            60,
            int,
        ),
        qa_series_delay_min_seconds=_env_or_value(
            "CHAT_AGENT_QA_SERIES_DELAY_MIN_SECONDS",
            getattr(args, "qa_series_delay_min_seconds", None),
            agent_cfg.get("qa_series_delay_min_seconds"),
            30,
            int,
        ),
        qa_series_delay_max_seconds=_env_or_value(
            "CHAT_AGENT_QA_SERIES_DELAY_MAX_SECONDS",
            getattr(args, "qa_series_delay_max_seconds", None),
            agent_cfg.get("qa_series_delay_max_seconds"),
            120,
            int,
        ),
    )

    return AgentConfig(
        openrouter=openrouter,
        classifier=classifier,
        system_prompt=system_prompt,
        reply_instruction=reply_instruction,
        max_history_messages=max_history_messages,
        period_days=period_days,
        only_invitations=getattr(args, "only_invitations", None)
        if getattr(args, "only_invitations", None) is not None
        else agent_cfg.get("only_invitations", False),
        dry_run=getattr(args, "dry_run", None)
        if getattr(args, "dry_run", None) is not None
        else (
            dry_run if dry_run is not None else agent_cfg.get("dry_run", False)
        ),
        limit=getattr(args, "limit", None) or agent_cfg.get("limit"),
        resume_id=getattr(args, "resume_id", None)
        or agent_cfg.get("resume_id"),
        skip_blacklisted=getattr(args, "skip_blacklisted", None)
        if getattr(args, "skip_blacklisted", None) is not None
        else agent_cfg.get("skip_blacklisted", True),
        force=getattr(args, "force", False),
        timing=timing,
    )
