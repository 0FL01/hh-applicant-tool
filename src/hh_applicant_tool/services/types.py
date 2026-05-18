from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from hh_applicant_tool.utils.string import bool2str

SearchSource = Literal["search", "similar"]
AnalysisStatus = Literal["ok", "blocked", "degraded"]
RecommendedAction = Literal["apply", "skip", "review"]
AttemptStatus = Literal["planned", "applied", "blocked", "failed", "unknown"]


@dataclass(frozen=True)
class SearchFilters:
    area: list[str] = field(default_factory=list)
    employment: list[str] = field(default_factory=list)
    experience: list[str] = field(default_factory=list)
    industry: list[str] = field(default_factory=list)
    professional_role: list[str] = field(default_factory=list)
    schedule: list[str] = field(default_factory=list)
    work_format: list[str] = field(default_factory=list)
    employer_id: list[str] = field(default_factory=list)
    excluded_employer_id: list[str] = field(default_factory=list)
    label: list[str] = field(default_factory=list)
    salary: int | None = None
    currency: str | None = "RUR"
    period: int | None = 7
    order_by: str | None = "publication_time"
    only_with_salary: bool = False
    premium: bool = False
    no_magic: bool = False
    per_page: int = 20
    page_limit: int = 1

    def to_params(
        self,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        scalar_fields = {
            "salary": self.salary,
            "currency": self.currency,
            "period": self.period,
            "order_by": self.order_by,
        }
        for key, value in scalar_fields.items():
            if value:
                params[key] = value

        list_fields = {
            "area": self.area,
            "employment": self.employment,
            "experience": self.experience,
            "industry": self.industry,
            "professional_role": self.professional_role,
            "schedule": self.schedule,
            "work_format": self.work_format,
            "employer_id": self.employer_id,
            "excluded_employer_id": self.excluded_employer_id,
            "label": self.label,
        }
        for key, value in list_fields.items():
            if value:
                params[key] = list(value)

        if self.only_with_salary:
            params["only_with_salary"] = bool2str(self.only_with_salary)
        if self.premium:
            params["premium"] = bool2str(self.premium)
        if self.no_magic:
            params["no_magic"] = bool2str(self.no_magic)

        return params


@dataclass(frozen=True)
class VacancySearchResult:
    source: SearchSource
    request_params: dict[str, Any]
    total_found: int
    items: list[dict[str, Any]]


@dataclass(frozen=True)
class VacancyDetailsResult:
    vacancy: dict[str, Any]


@dataclass(frozen=True)
class PrecheckResult:
    blocked: bool
    reasons: list[str] = field(default_factory=list)
    dedupe_key: str | None = None


@dataclass(frozen=True)
class VacancyAnalysisResult:
    analysis_id: str
    analysis_status: AnalysisStatus
    resume_id: str
    vacancy_id: str
    policy_hash: str
    model: str | None
    suitable: bool
    score: float
    reason: str
    red_flags: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    recommended_action: RecommendedAction = "review"
    precheck_reasons: list[str] = field(default_factory=list)
    dedupe_key: str | None = None


@dataclass(frozen=True)
class ApplicationAttemptResult:
    attempt_id: str
    status: AttemptStatus
    reason: str
    analysis_id: str
    dry_run: bool
    cover_letter_source: str
    cover_letter_preview: str | None
    policy_hash: str
    dedupe_key: str | None
    safety_blocks: list[str] = field(default_factory=list)
    vacancy: dict[str, Any] = field(default_factory=dict)
