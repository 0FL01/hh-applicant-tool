from __future__ import annotations

from ..models.application_attempt import ApplicationAttemptModel
from .base import BaseRepository


class ApplicationAttemptRepository(BaseRepository):
    __table__ = "application_attempts"
    model = ApplicationAttemptModel

    def count_applied_for_day(self, day_bucket: str) -> int:
        cur = self.conn.execute(
            f"""
            SELECT COUNT(*)
            FROM {self.table_name}
            WHERE day_bucket = ? AND status = 'applied' AND dry_run = 0
            """,
            (day_bucket,),
        )
        return int(cur.fetchone()[0])

    def latest_blocking_attempt(
        self,
        *,
        resume_id: str,
        vacancy_id: int | str | None = None,
        dedupe_key: str | None = None,
    ) -> ApplicationAttemptModel | None:
        if vacancy_id is None and dedupe_key is None:
            raise ValueError("vacancy_id or dedupe_key is required")

        params: dict[str, object] = {"resume_id": resume_id}
        where = "resume_id = :resume_id AND dry_run = 0 AND status IN ('applied', 'unknown')"
        if vacancy_id is not None:
            where += " AND vacancy_id = :vacancy_id"
            params["vacancy_id"] = int(vacancy_id)
        if dedupe_key is not None:
            where += " AND dedupe_key = :dedupe_key"
            params["dedupe_key"] = dedupe_key

        cur = self.conn.execute(
            f"SELECT * FROM {self.table_name} WHERE {where} ORDER BY created_at DESC LIMIT 1",
            params,
        )
        row = cur.fetchone()
        return self._row_to_model(cur, row) if row else None
