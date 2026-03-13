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
    "passive_update, marketing_broadcast, recruiter_contact_offer, "
    "system_event или irrelevant. "
    "reply ставь только если в последнем неотвеченном пакете есть прямой "
    "вопрос, screening, просьба подтвердить интерес, сообщить данные или "
    "выполнить следующий шаг. Нумерованные анкеты, скрининг-опросники, "
    "формулировки вида 'ответьте на несколько вопросов' и списки вопросов "
    "всегда считай human_actionable с action=reply, даже если сообщение "
    "выглядит шаблонным. recruiter_contact_offer ставь с action=skip, если "
    "рекрутер приглашает продолжить общение и оставляет прямые контакты "
    "вне hh.ru: email, telegram, телефон, ссылку на мессенджер или иной "
    "канал связи. skip ставь только для автоуведомлений, брендовых "
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
    request_interval_seconds: float = 0.35
    max_retries_on_rate_limit: int = 2
    rate_limit_retry_base_seconds: float = 2.0
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
class WebhookConfig:
    url: str
    enabled: bool = True
    timeout_seconds: float = 10.0
    verify_ssl: bool = True
    secret: str | None = None
    secret_header: str = "X-Webhook-Secret"
    max_attempts: int = 3
    retry_base_seconds: float = 30.0


@dataclass(frozen=True)
class AgentConfig:
    openrouter: OpenRouterConfig
    classifier: ClassifierConfig | None = None
    webhook: WebhookConfig | None = None
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


@dataclass(frozen=True)
class TelegramBotConfig:
    token: str
    target_chat_id: int
    base_url: str = "https://api.telegram.org"
    timeout_seconds: float = 10.0
    verify_ssl: bool = True


@dataclass(frozen=True)
class TelegramCollectorConfig:
    bot: TelegramBotConfig
    listen_host: str = "0.0.0.0"
    listen_port: int = 8787
    webhook_secret: str | None = None
    webhook_secret_header: str = "X-Webhook-Secret"
    db_path: str = "tg_collector.sqlite3"


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
    if arg_value is not None:
        return caster(arg_value)
    env_value = getenv(env_name)
    if env_value is not None:
        return caster(env_value)
    if config_value is not None:
        return caster(config_value)
    return default


def _env_bool_or_value(
    env_name: str,
    arg_value: bool | None,
    config_value: bool | None,
    default: bool,
) -> bool:
    if arg_value is not None:
        return bool(arg_value)
    env_value = _parse_env_bool(env_name)
    if env_value is not None:
        return env_value
    if config_value is not None:
        return bool(config_value)
    return default


def load_agent_config(tool: Any, args: Any) -> AgentConfig:
    config = tool.config

    openrouter_cfg = config.get("openrouter", {})
    agent_cfg = config.get("chat_agent", {})
    classifier_cfg = _get_nested(agent_cfg, "classifier", {}) or {}
    webhook_cfg = _get_nested(agent_cfg, "webhook", {}) or {}

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

    temperature = _env_or_value(
        "CHAT_AGENT_TEMPERATURE",
        getattr(args, "temperature", None),
        openrouter_cfg.get("temperature"),
        0.2,
        float,
    )

    max_completion_tokens = _env_or_value(
        "CHAT_AGENT_MAX_COMPLETION_TOKENS",
        getattr(args, "max_completion_tokens", None),
        openrouter_cfg.get("max_completion_tokens"),
        1200,
        int,
    )

    model = _env_or_value(
        "OPENROUTER_MODEL",
        getattr(args, "model", None),
        openrouter_cfg.get("model"),
        DEFAULT_OPENROUTER_MODEL,
        str,
    )

    base_url = _env_or_value(
        "OPENROUTER_BASE_URL",
        getattr(args, "openrouter_base_url", None),
        openrouter_cfg.get("base_url"),
        DEFAULT_OPENROUTER_BASE_URL,
        str,
    )

    period_days = _env_or_value(
        "CHAT_AGENT_PERIOD_DAYS",
        getattr(args, "period", None),
        agent_cfg.get("period_days"),
        None,
        int,
    )

    max_history_messages = _env_or_value(
        "CHAT_AGENT_MAX_HISTORY_MESSAGES",
        getattr(args, "max_history_messages", None),
        agent_cfg.get("max_history_messages"),
        12,
        int,
    )

    system_prompt = _env_or_value(
        "CHAT_AGENT_SYSTEM_PROMPT",
        getattr(args, "system_prompt", None),
        agent_cfg.get("system_prompt"),
        DEFAULT_SYSTEM_PROMPT,
        str,
    )

    reply_instruction = _env_or_value(
        "CHAT_AGENT_REPLY_INSTRUCTION",
        getattr(args, "reply_instruction", None),
        agent_cfg.get("reply_instruction"),
        DEFAULT_REPLY_INSTRUCTION,
        str,
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
        request_interval_seconds=_env_or_value(
            "CHAT_AGENT_OPENROUTER_REQUEST_INTERVAL_SECONDS",
            None,
            openrouter_cfg.get("request_interval_seconds"),
            0.35,
            float,
        ),
        max_retries_on_rate_limit=_env_or_value(
            "CHAT_AGENT_OPENROUTER_MAX_RETRIES_ON_RATE_LIMIT",
            None,
            openrouter_cfg.get("max_retries_on_rate_limit"),
            2,
            int,
        ),
        rate_limit_retry_base_seconds=_env_or_value(
            "CHAT_AGENT_OPENROUTER_RATE_LIMIT_RETRY_BASE_SECONDS",
            None,
            openrouter_cfg.get("rate_limit_retry_base_seconds"),
            2.0,
            float,
        ),
        reasoning_enabled=getattr(args, "reasoning", None)
        if getattr(args, "reasoning", None) is not None
        else openrouter_cfg.get("reasoning_enabled", True),
        app_name=openrouter_cfg.get("app_name") or "hh-applicant-tool",
        referer=(
            openrouter_cfg.get("referer")
            or "https://github.com/s3rgeym/hh-applicant-tool"
        ),
    )

    classifier_enabled = _env_bool_or_value(
        "CHAT_AGENT_CLASSIFIER_ENABLED",
        getattr(args, "classifier_enabled", None),
        classifier_cfg.get("enabled"),
        True,
    )

    classifier_temperature = _env_or_value(
        "CHAT_AGENT_CLASSIFIER_TEMPERATURE",
        getattr(args, "classifier_temperature", None),
        classifier_cfg.get("temperature"),
        0.0,
        float,
    )

    classifier_max_completion_tokens = _env_or_value(
        "CHAT_AGENT_CLASSIFIER_MAX_COMPLETION_TOKENS",
        getattr(args, "classifier_max_completion_tokens", None),
        classifier_cfg.get("max_completion_tokens"),
        256,
        int,
    )

    classifier_model = _env_or_value(
        "CHAT_AGENT_CLASSIFIER_MODEL",
        getattr(args, "classifier_model", None),
        classifier_cfg.get("model"),
        DEFAULT_CLASSIFIER_MODEL,
        str,
    )

    classifier_max_history_messages = _env_or_value(
        "CHAT_AGENT_CLASSIFIER_MAX_HISTORY_MESSAGES",
        getattr(args, "classifier_max_history_messages", None),
        classifier_cfg.get("max_history_messages"),
        8,
        int,
    )

    classifier_system_prompt = _env_or_value(
        "CHAT_AGENT_CLASSIFIER_SYSTEM_PROMPT",
        getattr(args, "classifier_system_prompt", None),
        classifier_cfg.get("system_prompt"),
        DEFAULT_CLASSIFIER_SYSTEM_PROMPT,
        str,
    )

    classifier_instruction = _env_or_value(
        "CHAT_AGENT_CLASSIFIER_INSTRUCTION",
        getattr(args, "classifier_instruction", None),
        classifier_cfg.get("instruction"),
        DEFAULT_CLASSIFIER_INSTRUCTION,
        str,
    )

    classifier_reasoning = _env_bool_or_value(
        "CHAT_AGENT_CLASSIFIER_REASONING",
        getattr(args, "classifier_reasoning", None),
        classifier_cfg.get("reasoning_enabled"),
        False,
    )

    classifier = ClassifierConfig(
        enabled=bool(classifier_enabled),
        openrouter=OpenRouterConfig(
            api_key=api_key,
            base_url=base_url,
            model=classifier_model,
            temperature=classifier_temperature,
            max_completion_tokens=classifier_max_completion_tokens,
            request_interval_seconds=openrouter.request_interval_seconds,
            max_retries_on_rate_limit=openrouter.max_retries_on_rate_limit,
            rate_limit_retry_base_seconds=openrouter.rate_limit_retry_base_seconds,
            reasoning_enabled=bool(classifier_reasoning),
            app_name=openrouter.app_name,
            referer=openrouter.referer,
        ),
        system_prompt=classifier_system_prompt,
        instruction=classifier_instruction,
        max_history_messages=classifier_max_history_messages,
    )

    webhook_url = _env_or_value(
        "CHAT_AGENT_WEBHOOK_URL",
        getattr(args, "webhook_url", None),
        webhook_cfg.get("url"),
        None,
        lambda value: str(value).strip() or None,
    )
    webhook_enabled = _env_bool_or_value(
        "CHAT_AGENT_WEBHOOK_ENABLED",
        getattr(args, "webhook_enabled", None),
        webhook_cfg.get("enabled"),
        bool(webhook_url),
    )
    webhook: WebhookConfig | None = None
    if webhook_enabled:
        if not webhook_url:
            raise ValueError(
                "Webhook is enabled but CHAT_AGENT_WEBHOOK_URL is not set."
            )
        webhook = WebhookConfig(
            url=webhook_url,
            enabled=True,
            timeout_seconds=_env_or_value(
                "CHAT_AGENT_WEBHOOK_TIMEOUT_SECONDS",
                getattr(args, "webhook_timeout_seconds", None),
                webhook_cfg.get("timeout_seconds"),
                10.0,
                float,
            ),
            verify_ssl=_env_bool_or_value(
                "CHAT_AGENT_WEBHOOK_VERIFY_SSL",
                getattr(args, "webhook_verify_ssl", None),
                webhook_cfg.get("verify_ssl"),
                True,
            ),
            secret=_env_or_value(
                "CHAT_AGENT_WEBHOOK_SECRET",
                getattr(args, "webhook_secret", None),
                webhook_cfg.get("secret"),
                None,
                lambda value: str(value) if value is not None else None,
            ),
            secret_header=_env_or_value(
                "CHAT_AGENT_WEBHOOK_SECRET_HEADER",
                getattr(args, "webhook_secret_header", None),
                webhook_cfg.get("secret_header"),
                "X-Webhook-Secret",
                str,
            ),
            max_attempts=_env_or_value(
                "CHAT_AGENT_WEBHOOK_MAX_ATTEMPTS",
                getattr(args, "webhook_max_attempts", None),
                webhook_cfg.get("max_attempts"),
                3,
                int,
            ),
            retry_base_seconds=_env_or_value(
                "CHAT_AGENT_WEBHOOK_RETRY_BASE_SECONDS",
                getattr(args, "webhook_retry_base_seconds", None),
                webhook_cfg.get("retry_base_seconds"),
                30.0,
                float,
            ),
        )

    quiet_hours_enabled = _env_bool_or_value(
        "CHAT_AGENT_QUIET_HOURS",
        getattr(args, "quiet_hours", None),
        agent_cfg.get("quiet_hours_enabled"),
        True,
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
        webhook=webhook,
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


def load_telegram_collector_config(
    tool: Any,
    args: Any,
) -> TelegramCollectorConfig:
    config = tool.config
    collector_cfg = config.get("telegram_collector", {}) or {}

    bot_token = _env_or_value(
        "TELEGRAM_COLLECTOR_BOT_TOKEN",
        getattr(args, "bot_token", None),
        collector_cfg.get("bot_token"),
        None,
        lambda value: str(value).strip() or None,
    )
    if not bot_token:
        raise ValueError(
            "Telegram bot token is not configured. Use telegram_collector.bot_token "
            "or TELEGRAM_COLLECTOR_BOT_TOKEN."
        )

    target_chat_id = _env_or_value(
        "TELEGRAM_COLLECTOR_TARGET_CHAT_ID",
        getattr(args, "target_chat_id", None),
        collector_cfg.get("target_chat_id"),
        None,
        int,
    )
    if target_chat_id is None:
        raise ValueError(
            "Telegram target chat id is not configured. Use "
            "telegram_collector.target_chat_id or TELEGRAM_COLLECTOR_TARGET_CHAT_ID."
        )

    db_path = _env_or_value(
        "TELEGRAM_COLLECTOR_DB_PATH",
        getattr(args, "db_path", None),
        collector_cfg.get("db_path"),
        str(tool.config_path / "tg_collector.sqlite3"),
        str,
    )

    return TelegramCollectorConfig(
        bot=TelegramBotConfig(
            token=bot_token,
            target_chat_id=int(target_chat_id),
            base_url=_env_or_value(
                "TELEGRAM_COLLECTOR_BOT_API_BASE_URL",
                getattr(args, "bot_api_base_url", None),
                collector_cfg.get("bot_api_base_url"),
                "https://api.telegram.org",
                str,
            ),
            timeout_seconds=_env_or_value(
                "TELEGRAM_COLLECTOR_TIMEOUT_SECONDS",
                getattr(args, "timeout_seconds", None),
                collector_cfg.get("timeout_seconds"),
                10.0,
                float,
            ),
            verify_ssl=_env_bool_or_value(
                "TELEGRAM_COLLECTOR_VERIFY_SSL",
                getattr(args, "verify_ssl", None),
                collector_cfg.get("verify_ssl"),
                True,
            ),
        ),
        listen_host=_env_or_value(
            "TELEGRAM_COLLECTOR_LISTEN_HOST",
            getattr(args, "host", None),
            collector_cfg.get("listen_host"),
            "0.0.0.0",
            str,
        ),
        listen_port=_env_or_value(
            "TELEGRAM_COLLECTOR_LISTEN_PORT",
            getattr(args, "port", None),
            collector_cfg.get("listen_port"),
            8787,
            int,
        ),
        webhook_secret=_env_or_value(
            "TELEGRAM_COLLECTOR_WEBHOOK_SECRET",
            getattr(args, "webhook_secret", None),
            collector_cfg.get("webhook_secret"),
            None,
            lambda value: str(value) if value is not None else None,
        ),
        webhook_secret_header=_env_or_value(
            "TELEGRAM_COLLECTOR_WEBHOOK_SECRET_HEADER",
            getattr(args, "webhook_secret_header", None),
            collector_cfg.get("webhook_secret_header"),
            "X-Webhook-Secret",
            str,
        ),
        db_path=db_path,
    )
