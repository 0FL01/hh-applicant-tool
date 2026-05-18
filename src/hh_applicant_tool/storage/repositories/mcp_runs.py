from __future__ import annotations

from datetime import UTC, datetime

from ..models.mcp_run import MCPRunModel
from .base import BaseRepository


class MCPRunRepository(BaseRepository):
    __table__ = "mcp_runs"
    model = MCPRunModel

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        total_candidates: int | None = None,
        analyzed_count: int | None = None,
        planned_apply_count: int | None = None,
        applied_count: int | None = None,
        skipped_count: int | None = None,
        blocked_count: int | None = None,
        error_count: int | None = None,
        commit: bool | None = None,
    ) -> None:
        updates = {
            "status": status,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        optional_counts = {
            "total_candidates": total_candidates,
            "analyzed_count": analyzed_count,
            "planned_apply_count": planned_apply_count,
            "applied_count": applied_count,
            "skipped_count": skipped_count,
            "blocked_count": blocked_count,
            "error_count": error_count,
        }
        updates.update(
            {key: value for key, value in optional_counts.items() if value is not None}
        )
        assignments = ", ".join(f"{key} = :{key}" for key in updates)
        self.conn.execute(
            f"UPDATE {self.table_name} SET {assignments} WHERE id = :id",
            {**updates, "id": run_id},
        )
        self.maybe_commit(commit)
