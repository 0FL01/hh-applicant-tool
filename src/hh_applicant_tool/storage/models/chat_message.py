from __future__ import annotations

from datetime import datetime

from .base import BaseModel, mapped


class ChatMessageModel(BaseModel):
    id: str
    negotiation_id: int
    chat_id: int | None = None
    participant_type: str = mapped(path="author.participant_type")
    text: str
    created_at: datetime | None = None
    viewed_by_opponent: bool | None = None
