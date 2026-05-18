from __future__ import annotations

from datetime import datetime

from .base import BaseModel


class ApplicationAttemptModel(BaseModel):
    id: str
    run_id: str | None = None
    analysis_id: str | None = None
    resume_id: str
    vacancy_id: int
    employer_id: int | None = None
    dedupe_key: str | None = None
    day_bucket: str
    dry_run: bool
    confirm_apply: bool = False
    status: str
    reason: str
    cover_letter_source: str
    cover_letter_preview: str | None = None
    cover_letter_sha256: str | None = None
    hh_request_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    sent_at: datetime | None = None
