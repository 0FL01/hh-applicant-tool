from __future__ import annotations

import hashlib
import html
import logging
import re
import uuid
from datetime import datetime
from typing import Any

import requests

from hh_applicant_tool.api.errors import Redirect
from hh_applicant_tool.api.errors import ApiError
from hh_applicant_tool.context import HHProfileContext
from hh_applicant_tool.storage.models.application_attempt import (
    ApplicationAttemptModel,
)
from hh_applicant_tool.storage.models.vacancy_analysis import (
    VacancyAnalysisModel,
)
from hh_applicant_tool.utils import json
from hh_applicant_tool.utils.string import strip_tags
from hh_llm_agent.openrouter import OpenRouterError, StructuredOutputSchema

from .cover_letter import (
    CoverLetterRequest,
    CoverLetterResult,
    hash_text,
    preview_text,
    render_template,
)
from .policy import VacancyPolicy
from .types import (
    ApplicationAttemptResult,
    PrecheckResult,
    SearchFilters,
    SearchSource,
    VacancyAnalysisResult,
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
        llm_client: Any | None = None,
        max_page_limit: int = 10,
        max_per_page: int = 100,
    ) -> None:
        self.context = context
        self.llm_client = llm_client
        self.max_page_limit = max_page_limit
        self.max_per_page = max_per_page
        self._vacancy_description_cache: dict[str, str] = {}

    VACANCY_ANALYSIS_SCHEMA = StructuredOutputSchema(
        name="vacancy_analysis",
        schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "suitable": {"type": "boolean"},
                "score": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
                "red_flags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "missing": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "recommended_action": {
                    "type": "string",
                    "enum": ["apply", "skip", "review"],
                },
            },
            "required": [
                "suitable",
                "score",
                "reason",
                "red_flags",
                "missing",
                "recommended_action",
            ],
        },
    )

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

    def record_precheck_analysis(
        self,
        *,
        resume_id: str,
        vacancy: dict[str, Any],
        precheck: PrecheckResult,
        policy: VacancyPolicy | None = None,
        run_id: str | None = None,
        source: str = "manual",
        source_query: str | None = None,
        analysis_mode: str = "light",
    ) -> VacancyAnalysisModel:
        policy = policy or VacancyPolicy()
        employer = vacancy.get("employer") or {}
        analysis = VacancyAnalysisModel(
            id=uuid.uuid4().hex,
            run_id=run_id,
            resume_id=resume_id,
            vacancy_id=int(vacancy["id"]),
            employer_id=int(employer["id"]) if employer.get("id") else None,
            dedupe_key=precheck.dedupe_key,
            source=source,
            source_query=source_query,
            analysis_status="blocked" if precheck.blocked else "ok",
            analysis_mode=analysis_mode,
            policy_hash=policy.hash(),
            policy_json=policy.to_canonical_dict(),
            suitable=not precheck.blocked,
            score=0.0 if precheck.blocked else 1.0,
            reason=(
                "Hard precheck blocked vacancy"
                if precheck.blocked
                else "Hard prechecks passed"
            ),
            red_flags_json=list(precheck.reasons),
            missing_json=[],
            recommended_action="skip" if precheck.blocked else "review",
            precheck_reasons_json=list(precheck.reasons),
            reasoning_details=[{"kind": "hard_precheck"}],
            vacancy_snapshot_json={
                "id": vacancy.get("id"),
                "name": vacancy.get("name"),
                "alternate_url": vacancy.get("alternate_url"),
                "employer": employer,
            },
        )
        self.storage.vacancy_analysis.save(analysis)
        return analysis

    def analyze_vacancy(
        self,
        *,
        resume_id: str,
        vacancy_id: str | int,
        policy: VacancyPolicy | None = None,
        analysis_mode: str = "light",
        run_id: str | None = None,
        source: str = "manual",
        source_query: str | None = None,
        work_format: list[str] | None = None,
    ) -> VacancyAnalysisResult:
        policy = policy or VacancyPolicy()
        vacancy = self.get_vacancy_details(vacancy_id).vacancy
        precheck = self.run_hard_prechecks(
            resume_id=resume_id,
            vacancy=vacancy,
            policy=policy,
            work_format=work_format,
        )
        if precheck.blocked:
            analysis = self.record_precheck_analysis(
                resume_id=resume_id,
                vacancy=vacancy,
                precheck=precheck,
                policy=policy,
                run_id=run_id,
                source=source,
                source_query=source_query,
                analysis_mode=analysis_mode,
            )
            return self._analysis_model_to_result(analysis)

        if self.llm_client is None:
            analysis = self._save_degraded_analysis(
                resume_id=resume_id,
                vacancy=vacancy,
                precheck=precheck,
                policy=policy,
                run_id=run_id,
                source=source,
                source_query=source_query,
                analysis_mode=analysis_mode,
                reason="LLM unavailable; only hard prechecks executed",
            )
            return self._analysis_model_to_result(analysis)

        messages = self._build_analysis_messages(
            resume_id=resume_id,
            vacancy=vacancy,
            policy=policy,
            analysis_mode=analysis_mode,
        )
        try:
            reply = self.llm_client.complete_json(
                messages,
                schema=self.VACANCY_ANALYSIS_SCHEMA,
            )
            parsed = reply.parsed or {}
        except (OpenRouterError, ValueError, TypeError) as ex:
            analysis = self._save_degraded_analysis(
                resume_id=resume_id,
                vacancy=vacancy,
                precheck=precheck,
                policy=policy,
                run_id=run_id,
                source=source,
                source_query=source_query,
                analysis_mode=analysis_mode,
                reason=f"LLM analysis unavailable: {ex}",
            )
            return self._analysis_model_to_result(analysis)

        normalized = self._normalize_analysis_payload(parsed, policy=policy)
        employer = vacancy.get("employer") or {}
        analysis = VacancyAnalysisModel(
            id=uuid.uuid4().hex,
            run_id=run_id,
            resume_id=resume_id,
            vacancy_id=int(vacancy["id"]),
            employer_id=int(employer["id"]) if employer.get("id") else None,
            dedupe_key=precheck.dedupe_key,
            source=source,
            source_query=source_query,
            analysis_status="ok",
            analysis_mode=analysis_mode,
            policy_hash=policy.hash(),
            policy_json=policy.to_canonical_dict(),
            suitable=normalized["suitable"],
            score=normalized["score"],
            reason=normalized["reason"],
            red_flags_json=normalized["red_flags"],
            missing_json=normalized["missing"],
            recommended_action=normalized["recommended_action"],
            precheck_reasons_json=[],
            model=getattr(getattr(self.llm_client, "config", None), "model", None),
            raw_response=reply.content,
            reasoning_details=reply.reasoning_details or [],
            vacancy_snapshot_json=self._vacancy_snapshot(vacancy),
        )
        self.storage.vacancy_analysis.save(analysis)
        return self._analysis_model_to_result(analysis)

    def generate_cover_letter(
        self,
        *,
        resume_id: str,
        vacancy: dict[str, Any],
        policy: VacancyPolicy | None = None,
        request: CoverLetterRequest | None = None,
    ) -> CoverLetterResult:
        policy = policy or VacancyPolicy()
        request = request or CoverLetterRequest()
        values = {
            "resume_id": resume_id,
            "vacancy_id": vacancy.get("id"),
            "vacancy_name": vacancy.get("name") or "",
            "employer_name": (vacancy.get("employer") or {}).get("name") or "",
            "cover_letter_style": policy.cover_letter_style,
            "cover_letter_language": policy.cover_letter_language,
        }

        if request.text:
            text = request.text.strip()
            return CoverLetterResult(
                source="provided",
                text=text,
                preview=preview_text(text),
                sha256=hash_text(text),
            )
        if request.template_text:
            text = render_template(request.template_text, values).strip()
            return CoverLetterResult(
                source="template",
                text=text,
                preview=preview_text(text),
                sha256=hash_text(text),
            )
        if request.mode == "llm" or (
            request.mode == "auto"
            and vacancy.get("response_letter_required")
            and self.llm_client is not None
        ):
            text = self.llm_client.send_message(
                (
                    "Составь короткое сопроводительное письмо для отклика на "
                    f"вакансию '{values['vacancy_name']}' в компании "
                    f"'{values['employer_name']}'. Стиль: {policy.cover_letter_style}. "
                    f"Язык: {policy.cover_letter_language}."
                )
            ).strip()
            return CoverLetterResult(
                source="llm",
                text=text,
                preview=preview_text(text),
                sha256=hash_text(text),
            )
        return CoverLetterResult(source="none", text="", preview=None, sha256=None)

    def apply_vacancy(
        self,
        *,
        resume_id: str,
        vacancy_id: str | int,
        policy: VacancyPolicy | None = None,
        cover_letter_request: CoverLetterRequest | None = None,
        analysis_id: str | None = None,
        analysis_mode: str = "light",
        dry_run: bool = True,
        confirm_apply: bool = False,
        allow_apply: bool = False,
        max_applications_per_day: int = 20,
        day_bucket: str | None = None,
        run_id: str | None = None,
    ) -> ApplicationAttemptResult:
        policy = policy or VacancyPolicy()
        day_bucket = day_bucket or datetime.now().date().isoformat()
        vacancy = self.get_vacancy_details(vacancy_id).vacancy
        analysis = (
            self._load_analysis_result(analysis_id)
            if analysis_id
            else self.analyze_vacancy(
                resume_id=resume_id,
                vacancy_id=vacancy_id,
                policy=policy,
                analysis_mode=analysis_mode,
                run_id=run_id,
            )
        )
        cover_letter = self.generate_cover_letter(
            resume_id=resume_id,
            vacancy=vacancy,
            policy=policy,
            request=cover_letter_request,
        )
        safety_blocks = self._apply_safety_blocks(
            analysis=analysis,
            dry_run=dry_run,
            confirm_apply=confirm_apply,
            allow_apply=allow_apply,
            vacancy=vacancy,
            cover_letter=cover_letter,
            day_bucket=day_bucket,
            max_applications_per_day=max_applications_per_day,
        )
        if safety_blocks:
            return self._record_attempt_result(
                run_id=run_id,
                analysis_id=analysis.analysis_id,
                resume_id=resume_id,
                vacancy=vacancy,
                dedupe_key=analysis.dedupe_key,
                day_bucket=day_bucket,
                dry_run=dry_run,
                confirm_apply=confirm_apply,
                status="blocked",
                reason=", ".join(safety_blocks),
                cover_letter=cover_letter,
                policy_hash=analysis.policy_hash,
                safety_blocks=safety_blocks,
            )

        if dry_run:
            return self._record_attempt_result(
                run_id=run_id,
                analysis_id=analysis.analysis_id,
                resume_id=resume_id,
                vacancy=vacancy,
                dedupe_key=analysis.dedupe_key,
                day_bucket=day_bucket,
                dry_run=True,
                confirm_apply=confirm_apply,
                status="planned",
                reason="dry-run planned apply",
                cover_letter=cover_letter,
                policy_hash=analysis.policy_hash,
                safety_blocks=[],
            )

        try:
            self.api_client.post(
                "/negotiations",
                {
                    "resume_id": resume_id,
                    "vacancy_id": vacancy_id,
                    "message": cover_letter.text,
                },
            )
        except Redirect:
            return self._record_attempt_result(
                run_id=run_id,
                analysis_id=analysis.analysis_id,
                resume_id=resume_id,
                vacancy=vacancy,
                dedupe_key=analysis.dedupe_key,
                day_bucket=day_bucket,
                dry_run=False,
                confirm_apply=confirm_apply,
                status="blocked",
                reason="manual_form_required",
                cover_letter=cover_letter,
                policy_hash=analysis.policy_hash,
                safety_blocks=["manual_form_required"],
            )
        except requests.RequestException as ex:
            return self._record_attempt_result(
                run_id=run_id,
                analysis_id=analysis.analysis_id,
                resume_id=resume_id,
                vacancy=vacancy,
                dedupe_key=analysis.dedupe_key,
                day_bucket=day_bucket,
                dry_run=False,
                confirm_apply=confirm_apply,
                status="unknown",
                reason=f"network outcome unknown: {ex}",
                cover_letter=cover_letter,
                policy_hash=analysis.policy_hash,
                safety_blocks=[],
            )

        if analysis.dedupe_key:
            employer = vacancy.get("employer") or {}
            self.storage.vacancy_response_dedup.remember(
                resume_id=resume_id,
                dedupe_key=analysis.dedupe_key,
                vacancy_id=vacancy_id,
                vacancy_name=vacancy.get("name") or "",
                employer_id=employer.get("id"),
                alternate_url=vacancy.get("alternate_url"),
            )
        return self._record_attempt_result(
            run_id=run_id,
            analysis_id=analysis.analysis_id,
            resume_id=resume_id,
            vacancy=vacancy,
            dedupe_key=analysis.dedupe_key,
            day_bucket=day_bucket,
            dry_run=False,
            confirm_apply=confirm_apply,
            status="applied",
            reason="applied",
            cover_letter=cover_letter,
            policy_hash=analysis.policy_hash,
            safety_blocks=[],
        )

    def _save_degraded_analysis(
        self,
        *,
        resume_id: str,
        vacancy: dict[str, Any],
        precheck: PrecheckResult,
        policy: VacancyPolicy,
        run_id: str | None,
        source: str,
        source_query: str | None,
        analysis_mode: str,
        reason: str,
    ) -> VacancyAnalysisModel:
        employer = vacancy.get("employer") or {}
        analysis = VacancyAnalysisModel(
            id=uuid.uuid4().hex,
            run_id=run_id,
            resume_id=resume_id,
            vacancy_id=int(vacancy["id"]),
            employer_id=int(employer["id"]) if employer.get("id") else None,
            dedupe_key=precheck.dedupe_key,
            source=source,
            source_query=source_query,
            analysis_status="degraded",
            analysis_mode=analysis_mode,
            policy_hash=policy.hash(),
            policy_json=policy.to_canonical_dict(),
            suitable=False,
            score=0.0,
            reason=reason,
            red_flags_json=[],
            missing_json=["llm_analysis"],
            recommended_action="review",
            precheck_reasons_json=[],
            reasoning_details=[{"kind": "degraded"}],
            vacancy_snapshot_json=self._vacancy_snapshot(vacancy),
        )
        self.storage.vacancy_analysis.save(analysis)
        return analysis

    def _build_analysis_messages(
        self,
        *,
        resume_id: str,
        vacancy: dict[str, Any],
        policy: VacancyPolicy,
        analysis_mode: str,
    ) -> list[dict[str, Any]]:
        resume_summary = self._build_resume_summary(
            resume_id=resume_id,
            analysis_mode=analysis_mode,
        )
        return [
            {
                "role": "system",
                "content": (
                    "Analyze whether this HeadHunter vacancy fits the resume "
                    "and policy. Return only the requested JSON schema."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "resume": resume_summary,
                        "vacancy": {
                            "id": vacancy.get("id"),
                            "name": vacancy.get("name"),
                            "description": self._get_vacancy_description(vacancy),
                            "employer": vacancy.get("employer") or {},
                            "snippet": vacancy.get("snippet") or {},
                        },
                        "policy": policy.to_canonical_dict(),
                    },
                    ensure_ascii=False,
                ),
            },
        ]

    def _build_resume_summary(
        self,
        *,
        resume_id: str,
        analysis_mode: str,
    ) -> dict[str, Any]:
        if analysis_mode == "heavy":
            resume = self.api_client.get(f"/resumes/{resume_id}")
            return {
                "id": resume.get("id", resume_id),
                "title": resume.get("title"),
                "skills": resume.get("skill_set") or [],
                "experience": resume.get("experience") or [],
            }
        for resume in self.context.get_resumes():
            if resume.get("id") == resume_id:
                return {
                    "id": resume_id,
                    "title": resume.get("title"),
                }
        return {"id": resume_id, "title": None}

    def _normalize_analysis_payload(
        self,
        payload: dict[str, Any],
        *,
        policy: VacancyPolicy,
    ) -> dict[str, Any]:
        score = max(0.0, min(1.0, float(payload.get("score") or 0.0)))
        recommended_action = str(payload.get("recommended_action") or "review")
        if recommended_action not in {"apply", "skip", "review"}:
            recommended_action = "review"
        if recommended_action == "apply" and score < policy.min_score:
            recommended_action = "skip"
        suitable = bool(payload.get("suitable")) and recommended_action == "apply"
        if recommended_action == "review":
            suitable = bool(payload.get("suitable"))
        return {
            "suitable": suitable,
            "score": score,
            "reason": str(payload.get("reason") or ""),
            "red_flags": [str(v) for v in payload.get("red_flags") or []],
            "missing": [str(v) for v in payload.get("missing") or []],
            "recommended_action": recommended_action,
        }

    def _analysis_model_to_result(
        self, analysis: VacancyAnalysisModel
    ) -> VacancyAnalysisResult:
        return VacancyAnalysisResult(
            analysis_id=analysis.id,
            analysis_status=analysis.analysis_status,
            resume_id=analysis.resume_id,
            vacancy_id=str(analysis.vacancy_id),
            policy_hash=analysis.policy_hash,
            model=analysis.model,
            suitable=analysis.suitable,
            score=analysis.score,
            reason=analysis.reason,
            red_flags=list(analysis.red_flags_json),
            missing=list(analysis.missing_json),
            recommended_action=analysis.recommended_action,
            precheck_reasons=list(analysis.precheck_reasons_json),
            dedupe_key=analysis.dedupe_key,
        )

    def _load_analysis_result(self, analysis_id: str) -> VacancyAnalysisResult:
        analysis = self.storage.vacancy_analysis.get(analysis_id)
        if analysis is None:
            raise ValueError(f"analysis_id not found: {analysis_id}")
        return self._analysis_model_to_result(analysis)

    def _apply_safety_blocks(
        self,
        *,
        analysis: VacancyAnalysisResult,
        dry_run: bool,
        confirm_apply: bool,
        allow_apply: bool,
        vacancy: dict[str, Any],
        cover_letter: CoverLetterResult,
        day_bucket: str,
        max_applications_per_day: int,
    ) -> list[str]:
        blocks: list[str] = []
        if not dry_run and not allow_apply:
            blocks.append("server_apply_disabled")
        if not dry_run and not confirm_apply:
            blocks.append("confirm_apply_required")
        if analysis.recommended_action != "apply":
            blocks.append("analysis_not_apply")
        if vacancy.get("response_letter_required") and not cover_letter.text:
            blocks.append("cover_letter_required_but_unavailable")
        if self.storage.application_attempts.count_applied_for_day(
            day_bucket
        ) >= max_applications_per_day:
            blocks.append("daily_limit_reached")
        if self.storage.application_attempts.latest_blocking_attempt(
            resume_id=analysis.resume_id,
            vacancy_id=analysis.vacancy_id,
        ):
            blocks.append("previous_apply_attempt")
        elif analysis.dedupe_key and self.storage.application_attempts.latest_blocking_attempt(
            resume_id=analysis.resume_id,
            dedupe_key=analysis.dedupe_key,
        ):
            blocks.append("previous_apply_attempt")
        return blocks

    def _record_attempt_result(
        self,
        *,
        run_id: str | None,
        analysis_id: str,
        resume_id: str,
        vacancy: dict[str, Any],
        dedupe_key: str | None,
        day_bucket: str,
        dry_run: bool,
        confirm_apply: bool,
        status: str,
        reason: str,
        cover_letter: CoverLetterResult,
        policy_hash: str,
        safety_blocks: list[str],
    ) -> ApplicationAttemptResult:
        employer = vacancy.get("employer") or {}
        attempt = ApplicationAttemptModel(
            id=uuid.uuid4().hex,
            run_id=run_id,
            analysis_id=analysis_id,
            resume_id=resume_id,
            vacancy_id=int(vacancy["id"]),
            employer_id=int(employer["id"]) if employer.get("id") else None,
            dedupe_key=dedupe_key,
            day_bucket=day_bucket,
            dry_run=dry_run,
            confirm_apply=confirm_apply,
            status=status,
            reason=reason,
            cover_letter_source=cover_letter.source,
            cover_letter_preview=cover_letter.preview,
            cover_letter_sha256=cover_letter.sha256,
        )
        self.storage.application_attempts.save(attempt)
        return ApplicationAttemptResult(
            attempt_id=attempt.id,
            status=status,
            reason=reason,
            analysis_id=analysis_id,
            dry_run=dry_run,
            cover_letter_source=cover_letter.source,
            cover_letter_preview=cover_letter.preview,
            policy_hash=policy_hash,
            dedupe_key=dedupe_key,
            safety_blocks=safety_blocks,
            vacancy={
                "id": str(vacancy.get("id")),
                "alternate_url": vacancy.get("alternate_url"),
            },
        )

    def _vacancy_snapshot(self, vacancy: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": vacancy.get("id"),
            "name": vacancy.get("name"),
            "alternate_url": vacancy.get("alternate_url"),
            "employer": vacancy.get("employer") or {},
        }

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
