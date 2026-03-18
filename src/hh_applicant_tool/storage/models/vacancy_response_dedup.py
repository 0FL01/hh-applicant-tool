from __future__ import annotations

from datetime import datetime

from .base import BaseModel


class VacancyResponseDedupModel(BaseModel):
    id: str | None = None
    resume_id: str
    dedupe_key: str
    employer_id: int | None = None
    vacancy_id: int
    vacancy_name: str
    alternate_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
