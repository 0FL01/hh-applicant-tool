from __future__ import annotations

from datetime import datetime

from .base import BaseModel


class SkippedVacancyModel(BaseModel):
    id: str | None = None
    resume_id: str
    vacancy_id: int
    employer_id: int | None = None
    vacancy_name: str = ""
    alternate_url: str | None = None
    reason: str = "ai_rejected"
    resume_analysis_mode: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
