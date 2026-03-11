from __future__ import annotations

from datetime import datetime

from .base import BaseModel


class AgentRunModel(BaseModel):
    id: str
    status: str
    model: str | None = None
    dry_run: bool = False
    total_negotiations: int = 0
    replied_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    created_at: datetime | None = None
    finished_at: datetime | None = None
