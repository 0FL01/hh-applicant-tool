from __future__ import annotations

from datetime import datetime

from .base import BaseModel, mapped


class AgentDecisionModel(BaseModel):
    id: str
    run_id: str | None = None
    negotiation_id: int
    chat_id: int | None = None
    vacancy_id: int | None = None
    employer_id: int | None = None
    resume_id: str | None = None
    last_message_id: str
    action: str
    reason: str | None = None
    reply_text: str | None = None
    model: str | None = None
    raw_response: str | None = None
    reasoning_details: list[dict] = mapped(
        default_factory=list, store_json=True
    )
    classifier_category: str | None = None
    classifier_reason: str | None = None
    classifier_confidence: float | None = None
    classifier_model: str | None = None
    classifier_raw_response: str | None = None
    created_at: datetime | None = None
