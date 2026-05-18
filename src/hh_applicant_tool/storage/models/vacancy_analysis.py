from __future__ import annotations

from datetime import datetime

from .base import BaseModel, mapped


class VacancyAnalysisModel(BaseModel):
    id: str
    run_id: str | None = None
    resume_id: str
    vacancy_id: int
    employer_id: int | None = None
    dedupe_key: str | None = None
    source: str
    source_query: str | None = None
    analysis_status: str
    analysis_mode: str
    policy_hash: str
    policy_json: dict = mapped(default_factory=dict, store_json=True)
    suitable: bool
    score: float
    reason: str
    red_flags_json: list[str] = mapped(default_factory=list, store_json=True)
    missing_json: list[str] = mapped(default_factory=list, store_json=True)
    recommended_action: str
    precheck_reasons_json: list[str] = mapped(
        default_factory=list,
        store_json=True,
    )
    model: str | None = None
    raw_response: str | None = None
    reasoning_details: list[dict] = mapped(
        default_factory=list,
        store_json=True,
    )
    prompt_hash: str | None = None
    vacancy_snapshot_json: dict = mapped(default_factory=dict, store_json=True)
    created_at: datetime | None = None
