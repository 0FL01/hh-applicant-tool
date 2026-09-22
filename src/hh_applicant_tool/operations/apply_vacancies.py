from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import logging
import random
import re
import time
from email.message import EmailMessage
from itertools import chain
from os import getenv
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import urlparse

import requests
from requests.cookies import create_cookie

from ..api import BadResponse, Redirect, datatypes
from ..api.datatypes import PaginatedItems, SearchVacancy
from ..api.errors import ApiError, CaptchaRequired, LimitExceeded
from hh_llm_agent.config import (
    DEFAULT_OPENROUTER_BASE_URL,
    OpenRouterConfig,
    load_openai_env,
    parse_reasoning_effort,
)
from hh_llm_agent.openrouter import OpenRouterChatClient, OpenRouterError
from ..main import BaseNamespace, BaseOperation
from ..storage.repositories.errors import RepositoryError
from ..utils.datatypes import VacancyTestsData
from ..utils.find import find_key
from ..utils.json import JSONDecoder
from ..utils.string import (
    bool2str,
    rand_text,
    strip_tags,
    unescape_string,
)

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)


class CaptchaSolveError(RuntimeError):
    """A CAPTCHA could not be solved safely; stop the current apply run."""


def validate_apply_delays(delay_min: float, delay_max: float) -> None:
    """Проверяет границы паузы между откликами (fail-fast до рассылки)."""
    if delay_min < 0 or delay_max < 0:
        raise ValueError(
            "--apply-delay-min/--apply-delay-max должны быть >= 0, "
            f"получено min={delay_min}, max={delay_max}"
        )
    if delay_min > delay_max:
        raise ValueError(
            "--apply-delay-min не может быть больше --apply-delay-max, "
            f"получено min={delay_min}, max={delay_max}"
        )


class Namespace(BaseNamespace):
    resume_id: str | None
    letter_file: Path | None
    ignore_employers: Path | None
    force_message: bool
    use_ai: bool
    first_prompt: str
    prompt: str
    order_by: str
    search: str
    schedule: str
    dry_run: bool
    # Пошли доп фильтры, которых не было
    experience: str
    employment: list[str] | None
    area: list[str] | None
    metro: list[str] | None
    professional_role: list[str] | None
    industry: list[str] | None
    employer_id: list[str] | None
    excluded_employer_id: list[str] | None
    currency: str | None
    salary: int | None
    only_with_salary: bool
    label: list[str] | None
    period: int | None
    date_from: str | None
    date_to: str | None
    top_lat: float | None
    bottom_lat: float | None
    left_lng: float | None
    right_lng: float | None
    sort_point_lat: float | None
    sort_point_lng: float | None
    no_magic: bool
    premium: bool
    per_page: int
    total_pages: int
    excluded_filter: str | None
    max_responses: int
    apply_delay_min: float
    apply_delay_max: float
    send_email: bool
    dedupe_vacancies: bool
    # HOTFIX(2026-04-07): Временное решение для обхода бага с парсингом тестов.
    # HH.ru изменил структуру HTML, и _get_vacancy_tests падает с "tests not found".
    # При включении этого флага вакансии с тестами пропускаются полностью.
    # TODO: Перейти на API endpoint или обновить маркеры парсинга.
    # См.: https://github.com/s3rgeym/hh-applicant-tool/commit/2d117c69930d065af3fb378ad7320060551c42ff
    skip_tests: bool
    work_format: list[str] | None
    ai_filter: str | None
    ai_rate_limit: int


class Operation(BaseOperation):
    """Откликнуться на все подходящие вакансии."""

    __aliases__ = ("apply", "apply-similar")
    excluded_keywords_env_name = "HH_APPLY_EXCLUDED_KEYWORDS"
    SEL_CAPTCHA_IMAGE = 'img[data-qa="account-captcha-picture"]'
    SEL_CAPTCHA_INPUT = 'input[data-qa="account-captcha-input"]'
    ai_filter: str | None = None
    ai_rate_limit: int = 0
    apply_delay_min: float = 30.0
    apply_delay_max: float = 120.0

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--resume-id", help="Идентефикатор резюме")
        parser.add_argument(
            "--search",
            help="Строка поиска для фильтрации вакансий. Если указана, то поиск будет производиться по вакансиям. В остальных случаях отклики будут производиться по списку рекомендованных вакансий.",  # noqa: E501
            type=str,
        )
        parser.add_argument(
            "-l",
            "-L",
            "--letter-file",
            "--letter",
            help="Путь до файла с текстом сопроводительного письма.",
            type=Path,
        )
        parser.add_argument(
            "-f",
            "--force-message",
            "--force",
            help="Всегда отправлять сообщение при отклике",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--use-ai",
            "--ai",
            help="Использовать AI для генерации сообщений",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--first-prompt",
            help="Начальный помпт чата для генерации сопроводительного письма",
            default="Напиши сопроводительное письмо для отклика на эту вакансию. Не используй placeholder'ы, твой ответ будет отправлен без обработки.",  # noqa: E501
        )
        parser.add_argument(
            "--prompt",
            help="Промпт для генерации сопроводительного письма",
            default="Сгенерируй сопроводительное письмо не более 5-7 предложений от моего имени для вакансии",  # noqa: E501
        )
        parser.add_argument(
            "--total-pages",
            "--pages",
            help="Количество обрабатываемых страниц поиска",  # noqa: E501
            default=20,
            type=int,
        )
        parser.add_argument(
            "--per-page",
            help="Сколько должно быть результатов на странице",  # noqa: E501
            default=100,
            type=int,
        )
        parser.add_argument(
            "--send-email",
            help="Отправлять письмо на email компании или рекрутера с просьбой рассмотреть резюме",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--excluded-filter",
            type=str,
            help=r"Исключить вакансии, если название или описание не соответствует шаблону. Например, `--excluded-filter 'junior|стажир|bitrix|дружн\w+ коллектив|полиграф|open\s*space|опенспейс|хакатон|конкурс|тестов\w+ задан'`",
        )
        parser.add_argument(
            "--max-responses",
            type=int,
            help="Остановить рассылку после N успешных откликов за запуск",
        )
        parser.add_argument(
            "--apply-delay-min",
            type=float,
            default=30.0,
            help="Минимальная пауза в секундах между успешными откликами (гуманизация темпа)",
        )
        parser.add_argument(
            "--apply-delay-max",
            type=float,
            default=120.0,
            help="Максимальная пауза в секундах между успешными откликами (гуманизация темпа)",
        )
        parser.add_argument(
            "--dry-run",
            help="Не отправлять отклики, а только выводить информацию",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--dedupe-vacancies",
            help="Пропускать вакансии-дубли с одинаковыми названием и описанием",
            default=True,
            action=argparse.BooleanOptionalAction,
        )
        # HOTFIX(2026-04-07): Флаг для пропуска вакансий с тестами.
        # Проблема: _get_vacancy_tests ищет маркеры 'vacancyTests' и 'counters' в HTML,
        # но HH.ru изменил структуру страницы (возможно, поле переименовалось или
        # данные теперь загружаются динамически через AJAX).
        # Временное решение: пропускать такие вакансии, чтобы не падать с ошибкой.
        # Полное решение требует реверс-инжиниринга новой структуры HH.ru.
        # Upstream issue: флаг добавлен в коммите 2d117c6 как workaround.
        parser.add_argument(
            "--skip-tests",
            help="Пропускать вакансии с тестами вместо попытки их решить (workaround для изменений в API HH.ru)",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--ai-filter",
            help="Использовать AI для фильтрации вакансий. Режимы: heavy/light",
            choices=["heavy", "light"],
            default=None,
        )
        parser.add_argument(
            "--ai-rate-limit",
            help="Лимит запросов к AI в минуту для фильтрации",
            type=int,
            default=40,
        )

        # Дальше идут параметры в точности соответствующие параметрам запроса
        # при поиске подходящих вакансий
        api_search_filters = parser.add_argument_group(
            "Фильтры для поиска вакансий",
            "Эти параметры напрямую соответствуют фильтрам поиска HeadHunter API",
        )

        api_search_filters.add_argument(
            "--order-by",
            help="Сортировка вакансий",
            choices=[
                "publication_time",
                "salary_desc",
                "salary_asc",
                "relevance",
                "distance",
            ],
            # default="relevance",
        )
        api_search_filters.add_argument(
            "--experience",
            help="Уровень опыта работы (noExperience, between1And3, between3And6, moreThan6)",
            type=str,
            default=None,
        )
        api_search_filters.add_argument(
            "--schedule",
            help="Тип графика (fullDay, shift, flexible, remote, flyInFlyOut)",
            type=str,
        )
        api_search_filters.add_argument(
            "--employment", nargs="+", help="Тип занятости"
        )
        api_search_filters.add_argument(
            "--area", nargs="+", help="Регион (area id)"
        )
        api_search_filters.add_argument(
            "--metro", nargs="+", help="Станции метро (metro id)"
        )
        api_search_filters.add_argument(
            "--professional-role", nargs="+", help="Проф. роль (id)"
        )
        api_search_filters.add_argument(
            "--industry", nargs="+", help="Индустрия (industry id)"
        )
        api_search_filters.add_argument(
            "--employer-id", nargs="+", help="ID работодателей"
        )
        api_search_filters.add_argument(
            "--excluded-employer-id", nargs="+", help="Исключить работодателей"
        )
        api_search_filters.add_argument(
            "--currency", help="Код валюты (RUR, USD, EUR)"
        )
        api_search_filters.add_argument(
            "--salary", type=int, help="Минимальная зарплата"
        )
        api_search_filters.add_argument(
            "--only-with-salary",
            default=False,
            action=argparse.BooleanOptionalAction,
        )
        api_search_filters.add_argument(
            "--label", nargs="+", help="Метки вакансий (label)"
        )
        api_search_filters.add_argument(
            "--period", type=int, help="Искать вакансии за N дней"
        )
        api_search_filters.add_argument(
            "--date-from", help="Дата публикации с (YYYY-MM-DD)"
        )
        api_search_filters.add_argument(
            "--date-to", help="Дата публикации по (YYYY-MM-DD)"
        )
        api_search_filters.add_argument(
            "--top-lat", type=float, help="Гео: верхняя широта"
        )
        api_search_filters.add_argument(
            "--bottom-lat", type=float, help="Гео: нижняя широта"
        )
        api_search_filters.add_argument(
            "--left-lng", type=float, help="Гео: левая долгота"
        )
        api_search_filters.add_argument(
            "--right-lng", type=float, help="Гео: правая долгота"
        )
        api_search_filters.add_argument(
            "--sort-point-lat",
            type=float,
            help="Координата lat для сортировки по расстоянию",
        )
        api_search_filters.add_argument(
            "--sort-point-lng",
            type=float,
            help="Координата lng для сортировки по расстоянию",
        )
        api_search_filters.add_argument(
            "--no-magic",
            action="store_true",
            help="Отключить авторазбор текста запроса",
        )
        api_search_filters.add_argument(
            "--premium",
            default=False,
            action=argparse.BooleanOptionalAction,
            help="Только премиум вакансии",
        )
        api_search_filters.add_argument(
            "--search-field",
            nargs="+",
            help="Поля поиска (name, company_name и т.п.)",
        )
        api_search_filters.add_argument(
            "--work-format",
            nargs="*",
            help="Формат работы (id из справочника: ON_SITE, REMOTE, HYBRID, FIELD_WORK, FLY_IN_FLY_OUT). "
            "По умолчанию: REMOTE. "
            "Пустое значение --work-format отключает фильтр.",
        )

    cover_letter: str = "{Здравствуйте|Добрый день}, меня зовут %(first_name)s. {Прошу|Предлагаю} рассмотреть {мою кандидатуру|мое резюме «%(resume_title)s»} на вакансию «%(vacancy_name)s». С уважением, %(first_name)s."

    @staticmethod
    def _parse_env_bool(value: str | None) -> bool | None:
        if value is None:
            return None

        normalized_value = value.strip().lower()
        if normalized_value in {"1", "true", "yes", "on"}:
            return True
        if normalized_value in {"0", "false", "no", "off"}:
            return False

        logger.warning(
            "Некорректное булево значение HH_APPLY_FORCE_MESSAGE=%r", value
        )
        return None

    @staticmethod
    def _parse_excluded_keywords(value: str | None) -> list[str]:
        if not value:
            return []

        keywords = [
            item.strip() for item in re.split(r"[,;\n]+", value) if item.strip()
        ]
        return list(dict.fromkeys(keywords))

    @staticmethod
    def _build_keywords_regex(keywords: list[str]) -> str | None:
        if not keywords:
            return None

        escaped = [
            re.escape(keyword).replace(r"\ ", r"\s+") for keyword in keywords
        ]
        return "|".join(escaped)

    @classmethod
    def _build_env_excluded_filter(
        cls,
        env_keywords: str | None,
    ) -> str | None:
        keywords = cls._parse_excluded_keywords(env_keywords)
        keywords_filter = cls._build_keywords_regex(keywords)
        if keywords_filter:
            logger.info(
                "Добавлен фильтр по %d ключевым словам из %s",
                len(keywords),
                cls.excluded_keywords_env_name,
            )
            return f"(?:{keywords_filter})"

        return None

    @property
    def api_client(self):
        return self.tool.api_client

    @property
    def args(self) -> Namespace:
        return self.tool.args

    def run(
        self,
        tool: HHApplicantTool,
    ) -> None | int:
        self.tool = tool
        args = self.args
        env_force_message = self._parse_env_bool(
            getenv("HH_APPLY_FORCE_MESSAGE")
        )
        env_cover_letter = getenv("HH_APPLY_COVER_LETTER")
        env_excluded_keywords = getenv(self.excluded_keywords_env_name)
        env_work_format = getenv("HH_APPLY_WORK_FORMAT")

        self.cover_letter = (
            args.letter_file.read_text(encoding="utf-8", errors="ignore")
            if args.letter_file
            else self.cover_letter
        )
        if env_cover_letter:
            self.cover_letter = env_cover_letter
        self.area = args.area
        self.bottom_lat = args.bottom_lat
        self.currency = args.currency
        self.date_from = args.date_from
        self.date_to = args.date_to
        self.dedupe_vacancies = args.dedupe_vacancies
        # HOTFIX(2026-04-07): Синхронизируем флаг пропуска тестов.
        # Причина: см. комментарий в Namespace.skip_tests и в setup_parser.
        self.skip_tests = args.skip_tests
        self.dry_run = args.dry_run
        self.employer_id = args.employer_id
        self.employment = args.employment
        self.excluded_employer_id = args.excluded_employer_id
        self.excluded_filter = args.excluded_filter
        self.excluded_keywords_filter = self._build_env_excluded_filter(
            env_keywords=env_excluded_keywords,
        )
        self.experience = args.experience
        # work_format: CLI > env > default(["REMOTE"])
        # --work-format без значений → [] → фильтр выключен
        # --work-format REMOTE HYBRID → ["REMOTE", "HYBRID"]
        # env HH_APPLY_WORK_FORMAT=REMOTE,HYBRID → ["REMOTE", "HYBRID"]
        # env HH_APPLY_WORK_FORMAT= → [] → фильтр выключен
        # ничего не указано → ["REMOTE"]
        cli_work_format = args.work_format
        if cli_work_format is not None:
            self.work_format = [v for v in cli_work_format if v]
        elif env_work_format is not None:
            self.work_format = [
                v.strip()
                for v in env_work_format.replace(";", ",").split(",")
                if v.strip()
            ]
        else:
            self.work_format = ["REMOTE"]
        self.force_message = (
            args.force_message
            if args.force_message is not None
            else bool(env_force_message)
        )
        self.industry = args.industry
        self.label = args.label
        self.left_lng = args.left_lng
        self.max_responses = args.max_responses
        # Гуманизация темпа: пауза random(min, max) после каждого успеха.
        self.apply_delay_min = args.apply_delay_min
        self.apply_delay_max = args.apply_delay_max
        validate_apply_delays(self.apply_delay_min, self.apply_delay_max)
        self.metro = args.metro
        self.no_magic = args.no_magic
        self.only_with_salary = args.only_with_salary
        self.order_by = args.order_by
        self.per_page = args.per_page
        self.period = args.period
        self.pre_prompt = args.prompt
        self.premium = args.premium
        self.professional_role = args.professional_role
        self.resume_id = args.resume_id
        self.right_lng = args.right_lng
        self.salary = args.salary
        self.schedule = args.schedule
        self.search = args.search
        self.search_field = args.search_field
        self.sort_point_lat = args.sort_point_lat
        self.sort_point_lng = args.sort_point_lng
        self.top_lat = args.top_lat
        self.total_pages = args.total_pages
        self._vacancy_description_cache: dict[str, str] = {}
        self._resume_analysis_cache: dict[tuple[str, str], str] = {}
        self.openai_chat = (
            tool.get_openai_chat(args.first_prompt) if args.use_ai else None
        )
        # AI filter
        self.ai_filter = args.ai_filter
        self.ai_rate_limit = args.ai_rate_limit
        # AI Vacancy Filter instance
        vf_config = tool.config.get("openai_vacancy_filter", {})
        openai_cfg = tool.config.get("openai", {})
        openai_env = load_openai_env()
        # api_key: openai_vacancy_filter.api_key → openai.token → universal api_key
        vf_token = (
            vf_config.get("api_key")
            or openai_cfg.get("token")
            or tool.config.get("api_key")
            or openai_env.api_key
        )
        # base_url: openai_vacancy_filter.base_url → openai.completion_endpoint → universal openai_base_url
        vf_base_url = (
            vf_config.get("base_url")
            or openai_cfg.get("completion_endpoint")
            or tool.config.get("openai_base_url")
            or openai_env.base_url
        )
        if self.ai_filter and not vf_token:
            raise ValueError(
                "Токен для AI-фильтрации не задан. "
                "Укажите openai_vacancy_filter.api_key в config.json, "
                "openai.token или универсальный api_key"
            )
        # Resolve proxies from tool.openai_session if available
        vf_proxies: dict[str, str] | None = None
        if hasattr(tool, "openai_session") and tool.openai_session.proxies:
            prox = tool.openai_session.proxies
            if prox.get("http") or prox.get("https"):
                vf_proxies = dict(prox)
        self._vacancy_filter_ai: OpenRouterChatClient | None = (
            OpenRouterChatClient(OpenRouterConfig(
                api_key=vf_token,
                base_url=vf_base_url,
                model=vf_config.get("model") or openai_env.model or "gpt-4o-mini",
                temperature=vf_config.get("temperature", 0.0),
                max_completion_tokens=vf_config.get("max_completion_tokens", 1000),
                reasoning_enabled=bool(vf_config.get("reasoning_enabled", False)),
                reasoning_effort=(
                    parse_reasoning_effort(
                        vf_config.get("reasoning_effort"),
                        "openai_vacancy_filter.reasoning_effort",
                    )
                    if vf_config.get("reasoning_effort") is not None
                    else openai_env.reasoning_effort
                ),
                proxies=vf_proxies,
            ))
            if self.ai_filter
            else None
        )
        self._captcha_ai: OpenRouterChatClient | None = None
        api_client = tool.api_client
        try:
            with api_client.handle_captcha(self._handle_captcha_required):
                self._apply_vacancies()
        except CaptchaRequired as ex:
            logger.error(
                "HH продолжает требовать CAPTCHA после одной попытки; "
                "рассылка остановлена (%s)",
                self._safe_captcha_url(ex.captcha_url),
            )
            return 1
        except CaptchaSolveError as ex:
            logger.error(
                "Не удалось решить CAPTCHA; рассылка остановлена: %s", ex
            )
            return 1

    def _apply_vacancies(self) -> None:
        resumes: list[datatypes.Resume] = self.tool.get_resumes()
        try:
            self.tool.storage.resumes.save_batch(resumes)
        except RepositoryError as ex:
            logger.exception(ex)
        resumes = (
            list(filter(lambda x: x["id"] == self.resume_id, resumes))
            if self.resume_id
            else resumes
        )
        # Выбираем только опубликованные
        resumes = list(
            filter(lambda x: x["status"]["id"] == "published", resumes)
        )
        if not resumes:
            logger.warning("У вас нет опубликованных резюме")
            return

        me: datatypes.User = self.tool.get_me()
        seen_employers = set()

        for resume in resumes:
            self._apply_resume(
                resume=resume,
                user=me,
                seen_employers=seen_employers,
            )

        # Синхронизация откликов
        # for neg in self.tool.get_negotiations():
        #     try:
        #         self.tool.storage.negotiations.save(neg)
        #     except RepositoryError as e:
        #         logger.warning(e)

        print("📝 Отклики на вакансии разосланы!")

    def _apply_resume(
        self,
        resume: datatypes.Resume,
        user: datatypes.User,
        seen_employers: set[str],
    ) -> None:
        logger.info(
            "Начинаю рассылку откликов для резюме: %s (%s)",
            resume["alternate_url"],
            resume["title"],
        )
        print("🚀 Начинаю рассылку откликов для резюме:", resume["title"])

        placeholders = {
            "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "",
            "email": user.get("email") or "",
            "phone": user.get("phone") or "",
            "resume_hash": resume.get("id") or "",
            "resume_title": resume.get("title") or "",
            "resume_url": resume.get("alternate_url") or "",
        }

        do_apply = True
        applied_count = 0
        storage = self.tool.storage
        site_emails = {}
        vacancies = list(self._get_vacancies(resume_id=resume["id"]))
        known_dedupe_vacancy_ids = self._load_known_dedupe_vacancy_ids(
            resume_id=resume["id"]
        )

        if self.dedupe_vacancies:
            self._seed_dedupe_from_existing_responses(
                resume_id=resume["id"],
                vacancies=vacancies,
                known_dedupe_vacancy_ids=known_dedupe_vacancy_ids,
            )

        for vacancy in vacancies:
            if (
                getattr(self, "_cancel_event", None)
                and self._cancel_event.is_set()
            ):
                logger.info("Операция отменена пользователем")
                break

            if self.max_responses and applied_count >= self.max_responses:
                logger.info(
                    "Достигнут лимит откликов --max-responses (%d). "
                    "Останавливаюсь.",
                    self.max_responses,
                )
                print(
                    f"🛑 Достигнут лимит откликов: {self.max_responses}. "
                    "Останавливаюсь."
                )
                break

            try:
                employer = vacancy.get("employer", {})
                employer_id = employer.get("id")

                message_placeholders = {
                    "vacancy_name": vacancy.get("name", ""),
                    "employer_name": employer.get("name", ""),
                    **placeholders,
                }

                try:
                    storage.vacancies.save(vacancy)
                except RepositoryError as ex:
                    logger.debug(ex)

                # По факту контакты можно получить только здесь?!
                if vacancy.get("contacts"):
                    logger.debug(
                        f"Найдены контакты в вакансии: {vacancy['alternate_url']}"
                    )

                    try:
                        # logger.debug(vacancy)
                        storage.vacancy_contacts.save(vacancy)
                    except RepositoryError as ex:
                        logger.exception(ex)

                if not do_apply:
                    continue

                vacancy_id = vacancy["id"]
                relations = vacancy.get("relations", [])

                dedupe_key = self._build_vacancy_dedupe_key(vacancy)

                if relations:
                    logger.debug(
                        "Пропускаем вакансию с откликом: %s",
                        vacancy["alternate_url"],
                    )
                    if "got_rejection" in relations:
                        logger.debug(
                            "Вы получили отказ от %s",
                            vacancy["alternate_url"],
                        )
                        print("⛔ Пришел отказ от", vacancy["alternate_url"])

                    self._remember_vacancy_dedupe(
                        resume_id=resume["id"],
                        vacancy=vacancy,
                        dedupe_key=dedupe_key,
                        known_dedupe_vacancy_ids=known_dedupe_vacancy_ids,
                        persist=not self.dry_run,
                    )
                    continue

                if dedupe_key and dedupe_key in known_dedupe_vacancy_ids:
                    logger.info(
                        "Пропускаем дубликат вакансии: %s (resume_id=%s, canonical_vacancy_id=%s)",
                        vacancy["alternate_url"],
                        resume["id"],
                        known_dedupe_vacancy_ids[dedupe_key],
                    )
                    continue

                if vacancy.get("archived"):
                    logger.debug(
                        "Пропускаем вакансию в архиве: %s",
                        vacancy["alternate_url"],
                    )
                    continue

                if redirect_url := vacancy.get("response_url"):
                    logger.debug(
                        "Пропускаем вакансию %s с перенаправлением: %s",
                        vacancy["alternate_url"],
                        redirect_url,
                    )
                    continue

                if self._is_work_format_filtered(vacancy):
                    logger.info(
                        "Пропускаем вакансию с неподходящим форматом работы: %s (work_format=%s, allowed=%s)",
                        vacancy["alternate_url"],
                        vacancy.get("work_format"),
                        self.work_format,
                    )
                    continue

                if (
                    self.excluded_filter
                    or self.excluded_keywords_filter
                ) and self._is_filtered(vacancy):
                    logger.info(
                        "Вакансия попала под фильтр: %s",
                        vacancy["alternate_url"],
                    )

                    self.api_client.put(
                        f"/vacancies/blacklisted/{vacancy['id']}"
                    )
                    logger.info(
                        "Вакансия добавлена в черный список: %s",
                        vacancy["alternate_url"],
                    )
                    continue

                # ── AI vacancy filter ─────────────────────────────────────
                if self.ai_filter:
                    resume_id = resume["id"]
                    vacancy_id = vacancy["id"]

                    if self._is_vacancy_already_skipped(resume_id, vacancy_id):
                        logger.info(
                            "Пропускаем ранее отклонённую AI вакансию: %s",
                            vacancy["alternate_url"],
                        )
                        continue

                    cache_key = (resume_id, self.ai_filter)
                    if cache_key not in self._resume_analysis_cache:
                        if self.ai_filter == "heavy":
                            analysis = self._analyze_resume_heavy(resume_id)
                        else:
                            analysis = self._analyze_resume_light(resume)
                        self._resume_analysis_cache[cache_key] = analysis
                    resume_analysis = self._resume_analysis_cache[cache_key]

                    vacancy_context = self._build_vacancy_context(vacancy, self.ai_filter)
                    suitable = self._ask_ai_suitability(vacancy_context, resume_analysis, self.ai_filter)

                    if not suitable:
                        logger.info(
                            "AI-фильтр отклонил вакансию: %s",
                            vacancy["alternate_url"],
                        )
                        if not self.dry_run:
                            self._save_skipped_vacancy(vacancy, resume_id)
                        continue

                    logger.debug(
                        "AI-фильтр пропустил вакансию: %s",
                        vacancy["alternate_url"],
                    )

                # Перед откликом выгружаем профиль компании
                if employer_id and employer_id not in seen_employers:
                    employer_profile: datatypes.Employer = self.api_client.get(
                        f"/employers/{employer_id}"
                    )
                    seen_employers.add(employer_id)

                    try:
                        storage.employers.save(employer_profile)
                    except RepositoryError as ex:
                        logger.exception(ex)

                    # Если есть сайт, то ищем на нем емейлы
                    if site_url := (
                        employer_profile.get("site_url") or ""
                    ).strip():
                        site_url = (
                            site_url
                            if "://" in site_url
                            else "https://" + site_url
                        )
                        logger.debug("visit site: %s", site_url)

                        try:
                            site_info = self._parse_site(site_url)
                            site_emails[employer_id] = site_info["emails"]
                        except requests.RequestException as ex:
                            site_info = None
                            logger.error(ex)

                        if site_info:
                            logger.debug("site info: %r", site_info)

                            # try:
                            #     subdomains = self._get_subdomains(site_url)
                            # except requests.RequestException as ex:
                            #     subdomains = []
                            #     logger.error(ex)

                            try:
                                storage.employer_sites.save(
                                    {
                                        "site_url": site_url,
                                        "employer_id": employer_id,
                                        "subdomains": [],
                                        **site_info,
                                    }
                                )
                            except RepositoryError as ex:
                                logger.exception(ex)

                letter = ""

                if self.force_message or vacancy.get(
                    "response_letter_required"
                ):
                    if self.openai_chat:
                        msg = self.pre_prompt + "\n\n"
                        msg += (
                            "Название вакансии: "
                            + message_placeholders["vacancy_name"]
                        )
                        msg += (
                            "Мое резюме:" + message_placeholders["resume_title"]
                        )
                        logger.debug("prompt: %s", msg)
                        letter = self.openai_chat.send_message(msg)
                    else:
                        try:
                            letter = (
                                rand_text(self.cover_letter)
                                % message_placeholders
                            )
                        except (KeyError, TypeError) as ex:
                            logger.warning(
                                "Не удалось подставить плейсхолдеры в сопроводительное письмо: %s. "
                                "Используется исходный текст без подстановки. "
                                "Доступные плейсхолдеры: %s",
                                ex,
                                list(message_placeholders.keys()),
                            )
                            letter = rand_text(self.cover_letter)

                    logger.debug(letter)

                logger.debug(
                    "Пробуем откликнуться на вакансию: %s",
                    vacancy["alternate_url"],
                )

                should_remember_dedupe = False

                # HOTFIX(2026-04-07) снят: парсинг тестов переписан на
                # HH-Lux-InitialState (см. _get_vacancy_tests). Если тест
                # все же не найден - падаем на обычный отклик через API,
                # а не пропускаем вакансию (upstream e742566).
                if vacancy.get("has_test") and self.skip_tests:
                    logger.debug(
                        "Пропускаю вакансию с тестом (флаг --skip-tests): %s",
                        vacancy["alternate_url"],
                    )
                    print(
                        "⏩ Пропущена вакансия с тестом:",
                        vacancy["alternate_url"],
                    )
                    continue

                test_handled = False

                if vacancy.get("has_test"):
                    logger.debug(
                        "Решаем тест: %s",
                        vacancy["alternate_url"],
                    )

                    try:
                        if not self.dry_run:
                            result = self._solve_vacancy_test(
                                vacancy_id=vacancy["id"],
                                resume_hash=resume["id"],
                                letter=letter,
                            )
                            if result.get("success") == "true":
                                print(
                                    "📨 Отправили отклик на вакансию с тестом",
                                    vacancy["alternate_url"],
                                )
                                should_remember_dedupe = True
                            else:
                                err = result.get("error")

                                if err == "negotiations-limit-exceeded":
                                    do_apply = False
                                    logger.warning("Достигли лимита на отклики")
                                else:
                                    logger.error(
                                        f"Произошла ошибка при отклике на вакансию с тестом: {vacancy['alternate_url']} - {err}"
                                    )
                        else:
                            should_remember_dedupe = True
                        test_handled = True
                    except ValueError as ex:
                        if str(ex) == "tests not found.":
                            # hh.ru не отдал данные теста (или тест отменен):
                            # пробуем откликнуться как на обычную вакансию.
                            logger.warning(
                                "Не удалось получить тест (%s), пробую откликнуться как на обычную вакансию: %s",
                                ex,
                                vacancy["alternate_url"],
                            )
                        else:
                            logger.error(
                                f"Произошла непредвиденная ошибка: {ex}"
                            )
                            continue
                    except (CaptchaRequired, CaptchaSolveError):
                        raise
                    except Exception as ex:
                        logger.error(f"Произошла непредвиденная ошибка: {ex}")
                        continue

                if not test_handled:
                    params = {
                        "resume_id": resume["id"],
                        "vacancy_id": vacancy_id,
                        "message": letter,
                    }
                    try:
                        if not self.dry_run:
                            res = self.api_client.post(
                                "/negotiations",
                                params,
                                delay=random.uniform(1, 3),
                            )
                            assert res == {}
                            print(
                                "📨 Отправили отклик на вакансию",
                                vacancy["alternate_url"],
                            )
                            should_remember_dedupe = True
                        else:
                            should_remember_dedupe = True
                    except Redirect:
                        logger.warning(
                            f"Игнорирую перенаправление на форму: {vacancy['alternate_url']}"  # noqa: E501
                        )
                        continue

                if should_remember_dedupe:
                    self._remember_vacancy_dedupe(
                        resume_id=resume["id"],
                        vacancy=vacancy,
                        dedupe_key=dedupe_key,
                        known_dedupe_vacancy_ids=known_dedupe_vacancy_ids,
                        persist=not self.dry_run,
                    )
                    # Считаем только реальные попытки отклика (включая тесты).
                    applied_count += 1
                    # Гуманизация темпа: случайная пауза между успехами.
                    # В dry-run не спим; перед выходом по лимиту тоже.
                    if not self.dry_run and not (
                        self.max_responses
                        and applied_count >= self.max_responses
                    ):
                        delay = random.uniform(
                            self.apply_delay_min, self.apply_delay_max
                        )
                        logger.debug(
                            "Пауза %.1fс перед следующим откликом", delay
                        )
                        cancel_event = getattr(self, "_cancel_event", None)
                        if cancel_event is not None:
                            if cancel_event.wait(delay):
                                logger.info("Операция отменена пользователем")
                                break
                        else:
                            time.sleep(delay)

                # Отправка письма на email
                if self.args.send_email:
                    mail_to: str | list[str] | None = vacancy.get(
                        "contacts", {}
                    ).get("email") or site_emails.get(employer_id)
                    if mail_to:
                        mail_to = (
                            ", ".join(mail_to)
                            if isinstance(mail_to, list)
                            else mail_to
                        )
                        mail_subject = rand_text(
                            self.tool.config.get("apply_mail_subject")
                            or "{Отклик|Резюме} на вакансию %(vacancy_name)s"
                        )
                        mail_body = unescape_string(
                            rand_text(
                                self.tool.config.get("apply_mail_body")
                                or "{Здравствуйте|Добрый день}, {прошу рассмотреть|пожалуйста рассмотрите} мое резюме %(resume_url)s на вакансию %(vacancy_name)s."
                                % message_placeholders
                            )
                        )
                        try:
                            self._send_email(mail_to, mail_subject, mail_body)
                            print(
                                "📧 Отправлено письмо на email по поводу вакансии",
                                vacancy["alternate_url"],
                            )
                        except Exception as ex:
                            logger.error(f"Ошибка отправки письма: {ex}")
            except LimitExceeded:
                do_apply = False
                logger.warning("Достигли лимита на отклики")
            except (CaptchaRequired, CaptchaSolveError):
                raise
            except ApiError as ex:
                logger.warning(ex)
            except (BadResponse, OpenRouterError) as ex:
                logger.error(ex)

        logger.info(
            "Закончили рассылку откликов для резюме: %s (%s)",
            resume["alternate_url"],
            resume["title"],
        )
        print("✅️ Закончили рассылку откликов для резюме:", resume["title"])

    @property
    def vacancy_filter_ai(self) -> OpenRouterChatClient:
        assert self._vacancy_filter_ai is not None
        return self._vacancy_filter_ai

    def _get_captcha_ai(self) -> OpenRouterChatClient:
        if self._captcha_ai is not None:
            return self._captcha_ai

        captcha_config = self.tool.config.get("openai_captcha", {})
        openai_config = self.tool.config.get("openai", {})
        openai_env = load_openai_env()
        api_key = (
            captcha_config.get("api_key")
            or openai_config.get("token")
            or self.tool.config.get("api_key")
            or openai_env.api_key
        )
        if not api_key:
            raise CaptchaSolveError(
                "Для CAPTCHA нужен API-ключ: задайте OPENAI_API_KEY "
                "или openai_captcha.api_key."
            )

        base_url = (
            captcha_config.get("base_url")
            or openai_config.get("completion_endpoint")
            or self.tool.config.get("openai_base_url")
            or openai_env.base_url
        )
        model = (
            captcha_config.get("model")
            or openai_config.get("model")
            or getenv("HH_CAPTCHA_MODEL")
            or openai_env.model
        )
        if not model:
            raise CaptchaSolveError(
                "Для CAPTCHA задайте vision-модель через OPENAI_MODEL "
                "или openai_captcha.model."
            )

        captcha_proxies: dict[str, str] | None = None
        if (
            hasattr(self.tool, "openai_session")
            and self.tool.openai_session.proxies
        ):
            proxies = self.tool.openai_session.proxies
            if proxies.get("http") or proxies.get("https"):
                captcha_proxies = dict(proxies)

        self._captcha_ai = OpenRouterChatClient(
            OpenRouterConfig(
                api_key=api_key,
                base_url=base_url or DEFAULT_OPENROUTER_BASE_URL,
                model=model,
                temperature=0.0,
                max_completion_tokens=20,
                reasoning_enabled=False,
                reasoning_effort=None,
                proxies=captcha_proxies,
            )
        )
        return self._captcha_ai

    @staticmethod
    def _safe_captcha_url(captcha_url: str | None) -> str:
        if not captcha_url:
            return "<URL отсутствует>"
        parsed = urlparse(captcha_url)
        if parsed.scheme != "https" or not parsed.hostname:
            return "<недопустимый URL CAPTCHA>"
        return f"https://{parsed.hostname}{parsed.path}"

    @staticmethod
    def _is_hh_cookie_domain(domain: str) -> bool:
        return bool(
            re.fullmatch(
                r"(?:[A-Za-z0-9-]+\.)*hh\.(?:ru|kz|uz|by|net|com)",
                domain.lstrip("."),
                flags=re.IGNORECASE,
            )
        )

    def _get_playwright_cookies(self) -> list[dict[str, Any]]:
        cookies: list[dict[str, Any]] = []
        for cookie in self.tool.session.cookies:
            if not cookie.domain or not self._is_hh_cookie_domain(
                cookie.domain
            ):
                continue
            if cookie.is_expired():
                continue

            playwright_cookie: dict[str, Any] = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
                "httpOnly": "HttpOnly" in getattr(cookie, "_rest", {}),
            }
            if cookie.expires is not None and cookie.expires > 0:
                playwright_cookie["expires"] = float(cookie.expires)
            same_site = getattr(cookie, "_rest", {}).get("SameSite")
            if same_site in {"Strict", "Lax", "None"}:
                playwright_cookie["sameSite"] = same_site
            cookies.append(playwright_cookie)
        return cookies

    def _merge_playwright_cookies(self, cookies: list[dict[str, Any]]) -> None:
        for cookie in cookies:
            domain = str(cookie.get("domain") or "").lstrip(".")
            if not domain or not self._is_hh_cookie_domain(domain):
                continue

            expires = cookie.get("expires")
            rest: dict[str, Any] = {}
            if cookie.get("httpOnly"):
                rest["HttpOnly"] = True
            if cookie.get("sameSite") in {"Strict", "Lax", "None"}:
                rest["SameSite"] = cookie["sameSite"]
            self.tool.session.cookies.set_cookie(
                create_cookie(
                    name=cookie["name"],
                    value=cookie["value"],
                    domain=cookie["domain"],
                    path=cookie.get("path") or "/",
                    secure=bool(cookie.get("secure")),
                    expires=(int(expires) if expires and expires > 0 else None),
                    rest=rest,
                )
            )

    def _handle_captcha_required(self, challenge: CaptchaRequired) -> None:
        captcha_url = challenge.captcha_url
        parsed = urlparse(captcha_url or "")
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if (
            parsed.scheme != "https"
            or parsed.hostname != "hh.ru"
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path.rstrip("/") != "/account/captcha"
        ):
            raise CaptchaSolveError(
                "HH вернул недопустимый адрес CAPTCHA; запрос не повторялся."
            )

        try:
            solved = asyncio.run(self._solve_hh_captcha_async(captcha_url))
        except CaptchaSolveError:
            raise
        except Exception as ex:
            raise CaptchaSolveError(
                f"Браузер CAPTCHA завершился ошибкой ({type(ex).__name__})."
            ) from ex

        if not solved:
            raise CaptchaSolveError("Vision-модель не распознала CAPTCHA.")
        logger.info(
            "CAPTCHA решена; один раз повторяю исходный API-запрос (%s)",
            self._safe_captcha_url(captcha_url),
        )

    async def _solve_hh_captcha_async(self, captcha_url: str) -> bool:
        try:
            from playwright.async_api import (
                TimeoutError as PlaywrightTimeoutError,
                async_playwright,
            )
        except ImportError as ex:
            raise CaptchaSolveError(
                "Для решения CAPTCHA нужны Playwright и Chromium."
            ) from ex

        captcha_ai = self._get_captcha_ai()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                api_user_agent = getattr(self.api_client, "user_agent", None)
                context_options = {"user_agent": api_user_agent} if api_user_agent else {}
                context = await browser.new_context(**context_options)
                existing_cookies = self._get_playwright_cookies()
                if existing_cookies:
                    await context.add_cookies(existing_cookies)

                page = await context.new_page()
                response = await page.goto(
                    captcha_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                if response is not None and response.status >= 400:
                    raise CaptchaSolveError(
                        f"Страница CAPTCHA вернула HTTP {response.status}."
                    )

                captcha_image = await page.wait_for_selector(
                    self.SEL_CAPTCHA_IMAGE,
                    timeout=10_000,
                    state="visible",
                )
                image_bytes = await captcha_image.screenshot()
                captcha_text = await asyncio.to_thread(
                    captcha_ai.solve_captcha,
                    image_bytes,
                )
                if not captcha_text:
                    raise CaptchaSolveError(
                        "Vision-модель вернула пустой текст CAPTCHA."
                    )

                await page.fill(self.SEL_CAPTCHA_INPUT, captcha_text)
                await page.press(self.SEL_CAPTCHA_INPUT, "Enter")
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=15_000,
                    )
                except PlaywrightTimeoutError:
                    # Some HH pages keep background requests open; success is
                    # established by retrying the original API request.
                    pass

                # Do not treat a still-visible CAPTCHA element as a failure:
                # some HH pages retain it after submission. The API retry below
                # is the authoritative check and is limited to one attempt.
                try:
                    page_feedback = await page.evaluate(
                        """() => {
                            const input = document.querySelector(
                                'input[data-qa="account-captcha-input"]'
                            );
                            const error = document.querySelector(
                                '[role="alert"], [data-qa*="captcha-error"]'
                            );
                            return {
                                inputInvalid:
                                    input?.getAttribute('aria-invalid') === 'true',
                                hasValidationAlert: Boolean(error),
                            };
                        }"""
                    )
                except Exception:
                    page_feedback = {}

                updated_cookies = await context.cookies()
                cookie_values_before = {
                    (
                        cookie.get("name"),
                        cookie.get("domain"),
                        cookie.get("path") or "/",
                    ): cookie.get("value")
                    for cookie in existing_cookies
                }
                changed_hh_cookie_count = sum(
                    1
                    for cookie in updated_cookies
                    if self._is_hh_cookie_domain(
                        str(cookie.get("domain") or "")
                    )
                    and cookie_values_before.get(
                        (
                            cookie.get("name"),
                            cookie.get("domain"),
                            cookie.get("path") or "/",
                        )
                    )
                    != cookie.get("value")
                )
                logger.info(
                    "CAPTCHA browser submission state: input_invalid=%s, "
                    "validation_alert=%s, HH_cookies_added_or_changed=%d",
                    page_feedback.get("inputInvalid"),
                    page_feedback.get("hasValidationAlert"),
                    changed_hh_cookie_count,
                )
                self._merge_playwright_cookies(updated_cookies)
                return True
            except CaptchaSolveError:
                raise
            except OpenRouterError as ex:
                raise CaptchaSolveError(f"Vision API error: {ex}") from ex
            except Exception as ex:
                raise CaptchaSolveError(
                    f"Не удалось пройти страницу CAPTCHA ({type(ex).__name__})."
                ) from ex
            finally:
                await browser.close()

    def _save_skipped_vacancy(self, vacancy: SearchVacancy, resume_id: str) -> None:
        employer = vacancy.get("employer", {})
        self.tool.storage.skipped_vacancies.remember(
            resume_id=resume_id,
            vacancy_id=vacancy["id"],
            vacancy_name=vacancy.get("name", ""),
            employer_id=employer.get("id") if employer else None,
            alternate_url=vacancy.get("alternate_url"),
            reason="ai_rejected",
            resume_analysis_mode=self.ai_filter,
        )

    def _is_vacancy_already_skipped(self, resume_id: str, vacancy_id: str) -> bool:
        return self.tool.storage.skipped_vacancies.is_skipped(resume_id, vacancy_id)

    def _analyze_resume_heavy(self, resume_id: str) -> str:
        """Full resume analysis: title, skills, full experience history."""
        cache_key = (resume_id, "heavy")
        if cache_key in self._resume_analysis_cache:
            return self._resume_analysis_cache[cache_key]
        resume = self.api_client.get(f"/resumes/{resume_id}")
        parts = [f"Название резюме: {resume.get('title', '')}"]
        if skills := resume.get("skills", ""):
            parts.append(f"Навыки: {skills}")
        if skill_set := resume.get("skill_set", []):
            parts.append(f"Технологии: {', '.join(s.strip() for s in skill_set if s.strip())}")
        experience = resume.get("experience", []) or []
        for exp in experience:
            company = exp.get("company", "")
            position = exp.get("position", "")
            period = f"{exp.get('start') or '?'} — {exp.get('end') or '?'}"
            desc = (exp.get("description") or "").strip()
            parts.append(f"• {company} | {position} ({period})")
            if desc:
                parts.append(f"  {desc[:500]}")
        text = "\n".join(parts)
        self._resume_analysis_cache[cache_key] = text
        return text

    @staticmethod
    def _analyze_resume_light(resume_obj: datatypes.Resume) -> str:
        """Lightweight resume summary from search-result data."""
        title = resume_obj.get("title", "")
        return f"Название резюме: {title}"

    def _build_vacancy_context(self, vacancy: SearchVacancy, mode: str) -> str:
        name = vacancy.get("name", "")
        employer = vacancy.get("employer", {}).get("name", "")
        if mode == "heavy":
            desc = self._get_vacancy_description(vacancy)
            desc = desc[:2000] if len(desc) > 2000 else desc
            return f"Название: {name}\nКомпания: {employer}\nОписание: {desc}"
        else:
            return f"Название: {name}\nКомпания: {employer}"

    @staticmethod
    def _build_filter_system_prompt(mode: str, resume_analysis: str) -> str:
        return (
            "Определи, подходит ли вакансия кандидату.\n"
            "Смотри в первую очередь на тип работы (роль), а не на технологии.\n"
            "Правила:\n"
            "1. Если работа по сути другая -> suitable = false\n"
            "2. Если роль совпадает или очень близкая:\n"
            "   - есть пересечения по задачам или навыкам -> suitable = true\n"
            "3. Общие технологии сами по себе ничего не значат.\n"
            "4. Если данных мало -> ориентируйся на название роли\n"
            "Не пиши объяснения.\n"
            "Ответ строго JSON:\n"
            '{"suitable": true} или {"suitable": false}\n'
            "\n"
            f"Кандидат:\n{resume_analysis}"
        )

    @staticmethod
    def _parse_ai_json_response(response: str) -> bool | None:
        response = response.strip().lower()
        if response in ("да", "yes", "true"):
            return True
        if response in ("нет", "no", "false"):
            return False
        m = re.search(
            r'\{\s*"suitable"\s*:\s*(true|false)\s*\}',
            response,
            re.IGNORECASE,
        )
        if m:
            return m.group(1).lower() == "true"
        if re.search(r'\bsuitable["\s]*:[^"]*\btrue\b', response, re.IGNORECASE):
            return True
        if re.search(r'\bsuitable["\s]*:[^"]*\bfalse\b', response, re.IGNORECASE):
            return False
        logger.warning("Could not parse AI filter response: %.200s", response)
        return None

    def _ask_ai_suitability(self, vacancy_context: str, resume_analysis: str, mode: str) -> bool:
        system_prompt = self._build_filter_system_prompt(mode, resume_analysis)
        vf_ai = self.vacancy_filter_ai
        for attempt in range(3):
            if self.ai_rate_limit > 0:
                delay = 60.0 / self.ai_rate_limit
                time.sleep(delay)
            try:
                response = vf_ai.send_message(
                    f"Вакансия:\n{vacancy_context}",
                    system_prompt=system_prompt,
                )
            except OpenRouterError:
                logger.warning("AI filter call failed, passing vacancy through")
                return True
            result = self._parse_ai_json_response(response)
            if result is not None:
                return result
            if attempt < 2:
                logger.debug("Retrying AI filter parse, attempt %d", attempt + 2)
                continue
        logger.warning("AI filter: could not parse response after 3 attempts, passing through")
        return True

    def _send_email(self, to: str, subject: str, body: str) -> None:
        cfg = self.tool.config.get("smtp", {})
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.get("from") or cfg.get("user")
        msg["To"] = to
        msg.set_content(body)
        self.tool.smtp.send_message(msg)

    def _load_known_dedupe_vacancy_ids(
        self, *, resume_id: str
    ) -> dict[str, int]:
        if not self.dedupe_vacancies:
            return {}

        return self.tool.storage.vacancy_response_dedup.list_vacancy_ids_by_key(
            resume_id
        )

    def _seed_dedupe_from_existing_responses(
        self,
        *,
        resume_id: str,
        vacancies: list[SearchVacancy],
        known_dedupe_vacancy_ids: dict[str, int],
    ) -> None:
        for vacancy in vacancies:
            if not vacancy.get("relations"):
                continue

            dedupe_key = self._build_vacancy_dedupe_key(vacancy)
            self._remember_vacancy_dedupe(
                resume_id=resume_id,
                vacancy=vacancy,
                dedupe_key=dedupe_key,
                known_dedupe_vacancy_ids=known_dedupe_vacancy_ids,
                persist=not self.dry_run,
            )

    def _remember_vacancy_dedupe(
        self,
        *,
        resume_id: str,
        vacancy: SearchVacancy,
        dedupe_key: str,
        known_dedupe_vacancy_ids: dict[str, int],
        persist: bool,
    ) -> None:
        if not self.dedupe_vacancies or not dedupe_key:
            return

        vacancy_id = int(vacancy["id"])
        known_dedupe_vacancy_ids.setdefault(dedupe_key, vacancy_id)

        if not persist:
            return

        employer_id = vacancy.get("employer", {}).get("id")
        self.tool.storage.vacancy_response_dedup.remember(
            resume_id=resume_id,
            dedupe_key=dedupe_key,
            vacancy_id=vacancy_id,
            vacancy_name=vacancy.get("name", ""),
            employer_id=int(employer_id) if employer_id else None,
            alternate_url=vacancy.get("alternate_url"),
        )

    def _build_vacancy_dedupe_key(self, vacancy: SearchVacancy) -> str:
        if not self.dedupe_vacancies:
            return ""

        employer_id = str(vacancy.get("employer", {}).get("id") or "")
        title = self._normalize_vacancy_text(vacancy.get("name") or "")
        description = self._normalize_vacancy_text(
            self._get_vacancy_description(vacancy)
        )
        payload = "\n".join((employer_id, title, description))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _get_vacancy_description(self, vacancy: SearchVacancy) -> str:
        vacancy_id = str(vacancy["id"])

        if vacancy_id in self._vacancy_description_cache:
            return self._vacancy_description_cache[vacancy_id]

        description = ""
        try:
            vacancy_details = self.api_client.get(f"/vacancies/{vacancy_id}")
            description = vacancy_details.get("description") or ""
        except CaptchaRequired:
            raise
        except ApiError as ex:
            logger.warning(
                "Не удалось загрузить описание вакансии %s: %s",
                vacancy.get("alternate_url"),
                ex,
            )

        if not description:
            snippet = vacancy.get("snippet") or {}
            description = "\n".join(
                filter(
                    None,
                    [
                        snippet.get("requirement"),
                        snippet.get("responsibility"),
                    ],
                )
            )

        self._vacancy_description_cache[vacancy_id] = description
        return description

    def _normalize_vacancy_text(self, value: str) -> str:
        value = html.unescape(value)
        value = value.replace("\xa0", " ").replace("\u200b", "")
        value = strip_tags(value)
        value = re.sub(r"\s+", " ", value)
        return value.strip().casefold()

    json_decoder = JSONDecoder()

    def _get_vacancy_tests(self, response_url: str) -> VacancyTestsData:
        """Парсит тесты из HH-Lux-InitialState на странице отклика.

        Старый линейный поиск JSON-маркеров ',"vacancyTests":' ... ',"counters":'
        перестал работать после смены структуры страницы hh.ru. Теперь
        конфигурация достается целиком из template#HH-Lux-InitialState,
        разэкранируется и разбирается как JSON, а vacancyTests ищется
        рекурсивно через find_key (upstream 67015f9/d0bc5e6).
        """
        res = self.tool.get_redirect_config(response_url)
        tests_data = find_key(res, "vacancyTests")
        if not tests_data:
            raise ValueError("tests not found.")
        return tests_data

    def _solve_vacancy_test(
        self,
        vacancy_id: str | int,
        resume_hash: str,
        letter: str = "",
    ) -> dict[str, Any]:
        """Загружает тест, ждет паузу и отправляет отклик."""
        response_url = f"https://hh.ru/applicant/vacancy_response?vacancyId={vacancy_id}&startedWithQuestion=false&hhtmFrom=vacancy"

        # Загружаем данные теста и токен
        tests_data = self._get_vacancy_tests(response_url)

        try:
            test_data = tests_data[str(vacancy_id)]
        except KeyError as ex:
            raise ValueError("Отсутствуют данные теста для вакансии.") from ex

        logger.debug(f"{test_data = }")

        payload: dict[str, Any] = {
            "_xsrf": self.tool.xsrf_token,
            "uidPk": test_data["uidPk"],
            "guid": test_data["guid"],
            "startTime": test_data["startTime"],
            "testRequired": test_data["required"],
            "vacancy_id": vacancy_id,
            "resume_hash": resume_hash,
            "ignore_postponed": "true",
            "incomplete": "false",
            "mark_applicant_visible_in_vacancy_country": "false",
            "country_ids": "[]",
            "lux": "true",
            "withoutTest": "no",
            "letter": letter,
        }

        for task in test_data["tasks"]:
            field_name = f"task_{task['id']}"
            solutions = task.get("candidateSolutions") or []
            question = (task.get("description") or "").strip()

            if solutions:
                if self.openai_chat:
                    options = "\n".join(
                        [
                            f"{s['id']}: {strip_tags(s['text'])}"
                            for s in solutions
                        ]
                    )
                    prompt = (
                        f"Вопрос: {question}\n"
                        f"Варианты:\n{options}\n"
                        f"Выбери ID правильного ответа. Пришли только ID."
                    )
                    ai_answer = self.openai_chat.send_message(prompt).strip()
                    # Ищем ID в ответе AI на случай лишнего текста
                    match = re.search(r"\d+", ai_answer)
                    selected_id = (
                        match.group(0) if match else solutions[0]["id"]
                    )
                    payload[field_name] = selected_id
                else:
                    # По статистике правильный ответ в большинстве случаев
                    # находится посередине
                    payload[field_name] = solutions[len(solutions) // 2]["id"]
            else:
                # Рандомные эмоджи
                # payload[f"{field_name}_text"] = "".join(
                #     chr(random.randint(0x1F300, 0x1F64F))
                #     for _ in range(random.randint(3, 15))
                # )

                if "://" in question:
                    answer = rand_text(
                        "{{Простите|Извините}, но я не перехожу по {внешним|сторонним} ссылкам, так как {опасаюсь взлома|не хочу {быть взломанным|подхватить вирус|чтобы у меня {со|с банковского} счета украли деньги}}.|У меня нет времени на заполнение анкет и гуглодоков}"
                    )
                elif self.openai_chat:
                    prompt = f"Дай краткий и профессиональный ответ на вопрос: {question}"
                    answer = self.openai_chat.send_message(prompt)
                # Тупоеблые любят вопросы с ответами да/нет, где ответ да является правильным в большинстве случаев.
                else:
                    answer = "Да"

                payload[f"{field_name}_text"] = answer

        logger.debug(f"{payload = }")

        # Ожидание перед отправкой (float)
        time.sleep(random.uniform(2.0, 3.0))

        response = self.tool.session.post(
            "https://hh.ru/applicant/vacancy_response/popup",
            data=payload,
            headers={
                "Referer": response_url,
                # x-gib-fgsscgib-w-hh и x-gib-gsscgib-w-hh вроде в куках
                # передаются и не нужны
                "X-Hhtmfrom": "vacancy",
                "X-Hhtmsource": "vacancy_response",
                "X-Requested-With": "XMLHttpRequest",
                "X-Xsrftoken": self.tool.xsrf_token,
            },
        )

        logger.debug(
            "%s %s %d",
            response.request.method,
            response.url,
            response.status_code,
        )

        data = response.json()
        # logger.debug(data)

        return data

    def _parse_site(self, url: str) -> dict[str, Any]:
        with self.tool.session.get(url, timeout=10) as r:
            val = lambda m: html.unescape(m.group(1)) if m else ""

            title = val(re.search(r"<title>(.*?)</title>", r.text, re.I | re.S))
            description = val(
                re.search(
                    r'<meta name="description" content="(.*?)"', r.text, re.I
                )
            )
            generator = val(
                re.search(
                    r'<meta name="generator" content="(.*?)"', r.text, re.I
                )
            )

            # Поиск email
            emails = set(
                re.findall(
                    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", r.text
                )
            )

            return {
                "title": title,
                "description": description,
                "generator": generator,
                "emails": list(emails),
                "server_name": r.headers.get("Server"),
                "powered_by": r.headers.get("X-Powered-By"),
                # Не работает, если отключена проверка сертификата
                "ip_address": r.raw._connection.sock.getpeername()[0]
                if r.raw._connection
                else None,
            }

    # Слишком тормознутая... Толи российские айпи заблокированы
    def _get_subdomains(self, url: str) -> set[str]:
        domain = urlparse(url).netloc
        r = self.tool.session.get(
            "https://crt.sh",
            params={"q": domain, "output": "json"},
            timeout=30,
        )

        r.raise_for_status()

        return set(
            item
            for item in chain.from_iterable(
                item["name_value"].split() for item in r.json()
            )
            if not item.startswith("*.")
        )

    def _get_search_params(self, page: int) -> dict:
        params = {
            "page": page,
            "per_page": self.per_page,
        }
        if self.order_by:
            params |= {"order_by": self.order_by}
        if self.search:
            params["text"] = self.search
        if self.schedule:
            params["schedule"] = self.schedule
        if self.experience:
            params["experience"] = self.experience
        if self.currency:
            params["currency"] = self.currency
        if self.salary:
            params["salary"] = self.salary
        if self.period:
            params["period"] = self.period
        if self.date_from:
            params["date_from"] = self.date_from
        if self.date_to:
            params["date_to"] = self.date_to
        if self.top_lat:
            params["top_lat"] = self.top_lat
        if self.bottom_lat:
            params["bottom_lat"] = self.bottom_lat
        if self.left_lng:
            params["left_lng"] = self.left_lng
        if self.right_lng:
            params["right_lng"] = self.right_lng
        if self.sort_point_lat:
            params["sort_point_lat"] = self.sort_point_lat
        if self.sort_point_lng:
            params["sort_point_lng"] = self.sort_point_lng
        if self.search_field:
            params["search_field"] = list(self.search_field)
        if self.employment:
            params["employment"] = list(self.employment)
        if self.area:
            params["area"] = list(self.area)
        if self.metro:
            params["metro"] = list(self.metro)
        if self.professional_role:
            params["professional_role"] = list(self.professional_role)
        if self.industry:
            params["industry"] = list(self.industry)
        if self.employer_id:
            params["employer_id"] = list(self.employer_id)
        if self.excluded_employer_id:
            params["excluded_employer_id"] = list(self.excluded_employer_id)
        if self.label:
            params["label"] = list(self.label)
        if self.only_with_salary:
            params["only_with_salary"] = bool2str(self.only_with_salary)
        # if self.clusters:
        #     params["clusters"] = bool2str(self.clusters)
        if self.no_magic:
            params["no_magic"] = bool2str(self.no_magic)
        if self.premium:
            params["premium"] = bool2str(self.premium)
        if self.work_format:
            params["work_format"] = self.work_format
        # if self.responses_count_enabled is not None:
        #     params["responses_count_enabled"] = bool2str(self.responses_count_enabled)

        return params

    def _get_vacancies(
        self, resume_id: str | None = None
    ) -> Iterator[SearchVacancy]:
        for page in range(self.total_pages):
            logger.debug(f"Загружаем вакансии со страницы: {page + 1}")
            params = self._get_search_params(page)

            if self.search:
                res: PaginatedItems[SearchVacancy] = self.api_client.get(
                    "/vacancies",
                    params,
                )
            else:
                res: PaginatedItems[SearchVacancy] = self.api_client.get(
                    f"/resumes/{resume_id}/similar_vacancies",
                    params,
                )

            logger.debug(f"Количество вакансий: {res['found']}")

            if not res["items"]:
                return

            yield from res["items"]

            if page >= res["pages"] - 1:
                return

    def _is_work_format_filtered(self, vacancy: SearchVacancy) -> bool:
        if not self.work_format:
            return False
        formats = vacancy.get("work_format") or []
        format_ids = {f.get("id") for f in formats if f.get("id")}
        if not format_ids:
            return False
        return not format_ids.intersection(self.work_format)

    def _is_filtered(self, vacancy: SearchVacancy) -> bool:
        # --max-responses больше не участвует в фильтрации: лимит
        # примененных откликов enforced в цикле рассылки (upstream 9f0e0d0).
        if not self.excluded_filter and not self.excluded_keywords_filter:
            return False

        if self.excluded_keywords_filter:
            keywords_pat: re.Pattern = re.compile(
                self.excluded_keywords_filter, re.IGNORECASE
            )
            # Проверяем и название вакансии, и название компании-работодателя.
            # Это позволяет отсекать вакансии от компаний вроде "Сбер", где
            # ключевое слово находится в имени работодателя, а не в тексте
            # самой вакансии.
            search_text = vacancy["name"]
            employer_name = vacancy.get("employer", {}).get("name", "")
            if employer_name:
                search_text += " " + employer_name
            if keywords_pat.search(search_text):
                return True

        if not self.excluded_filter:
            return False

        r = self.tool.session.get("https://hh.ru/vacancy/" + vacancy["id"])
        r.raise_for_status()
        # print(r.text)

        if self.excluded_filter:
            description = ""
            description_match = re.search(r'"description": (.*)', r.text)

            if description_match:
                try:
                    description, _ = self.json_decoder.raw_decode(
                        description_match.group(1)
                    )
                    description = strip_tags(description)
                except ValueError as ex:
                    logger.warning(
                        "Не удалось прочитать описание вакансии %s: %s",
                        vacancy["alternate_url"],
                        ex,
                    )

            content = vacancy["name"] + "\n" + description
            logger.debug(content[:2047])
            excluded_pat: re.Pattern = re.compile(
                self.excluded_filter, re.IGNORECASE
            )
            return bool(excluded_pat.search(content))

        return False
