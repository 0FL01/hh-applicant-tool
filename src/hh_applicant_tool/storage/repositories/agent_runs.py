from __future__ import annotations

from ..models.agent_run import AgentRunModel
from .base import BaseRepository


class AgentRunRepository(BaseRepository):
    __table__ = "agent_runs"
    model = AgentRunModel
