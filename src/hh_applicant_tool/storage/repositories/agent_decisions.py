from __future__ import annotations

from ..models.agent_decision import AgentDecisionModel
from .base import BaseRepository


class AgentDecisionRepository(BaseRepository):
    __table__ = "agent_decisions"
    model = AgentDecisionModel
    conflict_columns = ("negotiation_id", "last_message_id")
