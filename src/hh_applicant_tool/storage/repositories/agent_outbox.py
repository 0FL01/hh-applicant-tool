from __future__ import annotations

from datetime import datetime

from ..models.agent_outbox import AgentOutboxModel
from .base import BaseRepository


class AgentOutboxRepository(BaseRepository):
    __table__ = "agent_outbox"
    model = AgentOutboxModel
    conflict_columns = (
        "negotiation_id",
        "source_last_message_id",
        "sequence_no",
    )

    def list_pending(self) -> list[AgentOutboxModel]:
        cur = self.conn.execute(
            f"SELECT * FROM {self.table_name} WHERE status = ? "
            "ORDER BY send_after ASC, negotiation_id ASC, sequence_no ASC;",
            ("pending",),
        )
        return [self._row_to_model(cur, row) for row in cur.fetchall()]

    def list_pending_for_source(
        self,
        negotiation_id: int,
        source_last_message_id: str,
    ) -> list[AgentOutboxModel]:
        cur = self.conn.execute(
            f"SELECT * FROM {self.table_name} WHERE status = ? "
            "AND negotiation_id = ? AND source_last_message_id = ? "
            "ORDER BY sequence_no ASC;",
            ("pending", negotiation_id, source_last_message_id),
        )
        return [self._row_to_model(cur, row) for row in cur.fetchall()]

    def has_pending_for_source(
        self,
        negotiation_id: int,
        source_last_message_id: str,
    ) -> bool:
        cur = self.conn.execute(
            f"SELECT 1 FROM {self.table_name} WHERE status = ? "
            "AND negotiation_id = ? AND source_last_message_id = ? LIMIT 1;",
            ("pending", negotiation_id, source_last_message_id),
        )
        return cur.fetchone() is not None

    def cancel_pending_for_negotiation(
        self,
        negotiation_id: int,
        keep_source_last_message_id: str,
    ) -> None:
        self.conn.execute(
            f"UPDATE {self.table_name} SET status = ?, last_error = ? "
            "WHERE status = ? AND negotiation_id = ? AND source_last_message_id != ?;",
            (
                "stale",
                "superseded_by_new_employer_message",
                "pending",
                negotiation_id,
                keep_source_last_message_id,
            ),
        )
        self.maybe_commit()

    def mark_sent(self, outbox_id: str, sent_at: datetime) -> None:
        self.conn.execute(
            f"UPDATE {self.table_name} SET status = ?, sent_at = ?, last_error = NULL "
            "WHERE id = ?;",
            ("sent", sent_at.isoformat(), outbox_id),
        )
        self.maybe_commit()

    def mark_failed(self, outbox_id: str, error: str) -> None:
        self.conn.execute(
            f"UPDATE {self.table_name} SET status = ?, last_error = ? WHERE id = ?;",
            ("failed", error, outbox_id),
        )
        self.maybe_commit()
