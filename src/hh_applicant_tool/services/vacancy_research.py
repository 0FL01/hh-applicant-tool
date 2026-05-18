from __future__ import annotations

import hashlib
import html
import logging
import re
from typing import Any

from hh_applicant_tool.api.errors import ApiError
from hh_applicant_tool.context import HHProfileContext
from hh_applicant_tool.utils.string import strip_tags

from .policy import VacancyPolicy
from .types import (
    PrecheckResult,
    SearchFilters,
    SearchSource,
    VacancyDetailsResult,
    VacancySearchResult,
)

logger = logging.getLogger(__package__)


class VacancyResearchService:
    """Programmatic vacancy research core used by MCP and future adapters."""

    def __init__(
        self,
        context: HHProfileContext,
        *,
        max_page_limit: int = 10,
        max_per_page: int = 100,
    ) -> None:
        self.context = context
        self.max_page_limit = max_page_limit
        self.max_per_page = max_per_page
        self._vacancy_description_cache: dict[str, str] = {}

    @property
    def api_client(self):
        return self.context.api_client

    @property
    def storage(self):
        return self.context.storage

    def search_vacancies(
        self,
        *,
        search: str,
        filters: SearchFilters | None = None,
    ) -> VacancySearchResult:
        if not search:
            raise ValueError("search is required for vacancy search")
        filters = filters or SearchFilters()
        return self._fetch_vacancies(
            source="search",
            endpoint="/vacancies",
            filters=filters,
            extra_params={"text": search},
        )

    def get_similar_vacancies(
        self,
        *,
        resume_id: str,
        filters: SearchFilters | None = None,
    ) -> VacancySearchResult:
        if not resume_id:
            raise ValueError("resume_id is required for similar vacancies")
        filters = filters or SearchFilters()
        return self._fetch_vacancies(
            source="similar",
            endpoint=f"/resumes/{resume_id}/similar_vacancies",
            filters=filters,
            extra_params={},
        )

    def get_vacancy_details(self, vacancy_id: str | int) -> VacancyDetailsResult:
        vacancy = self.api_client.get(f"/vacancies/{vacancy_id}")
        self.storage.vacancies.save(vacancy)
        return VacancyDetailsResult(vacancy=vacancy)

    def _fetch_vacancies(
        self,
        *,
        source: SearchSource,
        endpoint: str,
        filters: SearchFilters,
        extra_params: dict[str, Any],
    ) -> VacancySearchResult:
        page_limit = max(1, min(filters.page_limit, self.max_page_limit))
        per_page = max(1, min(filters.per_page, self.max_per_page))
        all_items: list[dict[str, Any]] = []
        total_found = 0
        first_params: dict[str, Any] | None = None

        for page in range(page_limit):
            params = filters.to_params(page=page, per_page=per_page)
            params.update(extra_params)
            if first_params is None:
                first_params = dict(params)

            result = self.api_client.get(endpoint, params)
            total_found = int(result.get("found") or total_found)
            items = list(result.get("items") or [])
            if items:
                self.storage.vacancies.save_batch(items)
                all_items.extend(items)

            if not items or page + 1 >= int(result.get("pages") or 0):
                break

        return VacancySearchResult(
            source=source,
            request_params=first_params or {},
            total_found=total_found,
            items=all_items,
        )

    def build_vacancy_dedupe_key(self, vacancy: dict[str, Any]) -> str:
        employer_id = str((vacancy.get("employer") or {}).get("id") or "")
        title = self._normalize_vacancy_text(vacancy.get("name") or "")
        description = self._normalize_vacancy_text(
            self._get_vacancy_description(vacancy)
        )
        payload = "\n".join((employer_id, title, description))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def run_hard_prechecks(
        self,
        *,
        resume_id: str,
        vacancy: dict[str, Any],
        policy: VacancyPolicy | None = None,
        work_format: list[str] | None = None,
        known_dedupe_keys: dict[str, int] | None = None,
    ) -> PrecheckResult:
        policy = policy or VacancyPolicy()
        reasons: list[str] = []

        if vacancy.get("archived"):
            reasons.append("archived")
        if vacancy.get("response_url") or vacancy.get("adv_response_url"):
            reasons.append("manual_form_required")
        if vacancy.get("has_test"):
            reasons.append("has_test")
        if vacancy.get("relations"):
            reasons.append("already_has_relations")

        employer = vacancy.get("employer") or {}
        employer_id = str(employer.get("id") or "")
        employer_name = str(employer.get("name") or "")

        excluded_employers = {
            str(item).casefold() for item in policy.excluded_employers
        }
        if employer_id and employer_id.casefold() in excluded_employers:
            reasons.append("excluded_employer")
        if employer_name and employer_name.casefold() in excluded_employers:
            reasons.append("excluded_employer")

        search_text = f"{vacancy.get('name') or ''} {employer_name}".casefold()
        if any(
            str(keyword).casefold() in search_text
            for keyword in policy.excluded_keywords
        ):
            reasons.append("excluded_keyword")

        if self._is_work_format_blocked(vacancy, work_format):
            reasons.append("work_format_mismatch")

        dedupe_key = self.build_vacancy_dedupe_key(vacancy)
        known_dedupe_keys = known_dedupe_keys or {}
        if dedupe_key in known_dedupe_keys:
            reasons.append("dedupe_hit")
        elif self.storage.vacancy_response_dedup.exists(
            resume_id=resume_id,
            dedupe_key=dedupe_key,
        ):
            reasons.append("dedupe_hit")

        return PrecheckResult(
            blocked=bool(reasons),
            reasons=reasons,
            dedupe_key=dedupe_key,
        )

    def _get_vacancy_description(self, vacancy: dict[str, Any]) -> str:
        vacancy_id = str(vacancy["id"])
        if vacancy_id in self._vacancy_description_cache:
            return self._vacancy_description_cache[vacancy_id]

        description = ""
        try:
            vacancy_details = self.api_client.get(f"/vacancies/{vacancy_id}")
            description = vacancy_details.get("description") or ""
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

    def _is_work_format_blocked(
        self,
        vacancy: dict[str, Any],
        work_format: list[str] | None,
    ) -> bool:
        if not work_format:
            return False
        formats = vacancy.get("work_format") or []
        format_ids = {f.get("id") for f in formats if f.get("id")}
        if not format_ids:
            return False
        return not format_ids.intersection(work_format)
