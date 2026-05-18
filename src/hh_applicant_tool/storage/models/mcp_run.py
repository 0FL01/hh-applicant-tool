from __future__ import annotations

from datetime import datetime

from .base import BaseModel, mapped


class MCPRunModel(BaseModel):
    id: str
    tool_name: str
    status: str
    transport: str = "stdio"
    profile_id: str
    resume_id: str | None = None
    dry_run: bool = True
    confirm_apply: bool = False
    policy_hash: str
    policy_json: dict = mapped(default_factory=dict, store_json=True)
    search_params_json: dict = mapped(default_factory=dict, store_json=True)
    model: str | None = None
    total_candidates: int = 0
    analyzed_count: int = 0
    planned_apply_count: int = 0
    applied_count: int = 0
    skipped_count: int = 0
    blocked_count: int = 0
    error_count: int = 0
    created_at: datetime | None = None
    finished_at: datetime | None = None
