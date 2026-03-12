from __future__ import annotations

from datetime import datetime

from ..models.agent_webhook import AgentWebhookModel
from .base import BaseRepository


class AgentWebhookRepository(BaseRepository):
    __table__ = "agent_webhooks"
    model = AgentWebhookModel
    conflict_columns = (
        "negotiation_id",
        "source_last_message_id",
        "event_type",
    )

    def list_pending(self) -> list[AgentWebhookModel]:
        cur = self.conn.execute(
            f"SELECT * FROM {self.table_name} WHERE status = ? "
            "ORDER BY send_after ASC, negotiation_id ASC;",
            ("pending",),
        )
        return [self._row_to_model(cur, row) for row in cur.fetchall()]

    def list_pending_for_source(
        self,
        negotiation_id: int,
        source_last_message_id: str,
    ) -> list[AgentWebhookModel]:
        cur = self.conn.execute(
            f"SELECT * FROM {self.table_name} WHERE status = ? "
            "AND negotiation_id = ? AND source_last_message_id = ? "
            "ORDER BY send_after ASC, created_at ASC;",
            ("pending", negotiation_id, source_last_message_id),
        )
        return [self._row_to_model(cur, row) for row in cur.fetchall()]

    def mark_sent(self, webhook_id: str, sent_at: datetime) -> None:
        self.conn.execute(
            f"UPDATE {self.table_name} SET status = ?, sent_at = ?, last_error = NULL "
            "WHERE id = ?;",
            ("sent", sent_at.isoformat(), webhook_id),
        )
        self.maybe_commit()

    def mark_failed(
        self,
        webhook_id: str,
        *,
        attempts_count: int,
        error: str,
    ) -> None:
        self.conn.execute(
            f"UPDATE {self.table_name} SET status = ?, attempts_count = ?, last_error = ? "
            "WHERE id = ?;",
            ("failed", attempts_count, error, webhook_id),
        )
        self.maybe_commit()

    def reschedule(
        self,
        webhook_id: str,
        *,
        attempts_count: int,
        error: str,
        send_after: datetime,
    ) -> None:
        self.conn.execute(
            f"UPDATE {self.table_name} SET status = ?, attempts_count = ?, last_error = ?, send_after = ? "
            "WHERE id = ?;",
            (
                "pending",
                attempts_count,
                error,
                send_after.isoformat(),
                webhook_id,
            ),
        )
        self.maybe_commit()
