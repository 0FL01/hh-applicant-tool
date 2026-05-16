from __future__ import annotations

from datetime import datetime

from .base import BaseModel


class SkippedVacancyModel(BaseModel):
    id: str | None = None
    resume_id: str
    vacancy_id: str
    reason: str
    alternate_url: str | None = None
    name: str | None = None
    employer_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
