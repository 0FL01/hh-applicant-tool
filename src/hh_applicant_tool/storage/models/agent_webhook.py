from __future__ import annotations

from datetime import datetime

from .base import BaseModel, mapped


class AgentWebhookModel(BaseModel):
    id: str
    run_id: str | None = None
    negotiation_id: int
    chat_id: int | None = None
    source_last_message_id: str
    event_type: str
    target_url: str
    payload_json: dict[str, object] = mapped(
        store_json=True, default_factory=dict
    )
    send_after: datetime | None = None
    sent_at: datetime | None = None
    status: str = "pending"
    attempts_count: int = 0
    last_error: str | None = None
    created_at: datetime | None = None
