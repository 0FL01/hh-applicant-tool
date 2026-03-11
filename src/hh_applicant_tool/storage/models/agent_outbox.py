from __future__ import annotations

from datetime import datetime

from .base import BaseModel


class AgentOutboxModel(BaseModel):
    id: str
    run_id: str | None = None
    negotiation_id: int
    chat_id: int | None = None
    source_last_message_id: str
    sequence_no: int
    message_text: str
    send_after: datetime | None = None
    sent_at: datetime | None = None
    status: str = "pending"
    last_error: str | None = None
    created_at: datetime | None = None
