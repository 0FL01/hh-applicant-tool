from __future__ import annotations

from typing import Any

from ..models.skipped_vacancy import SkippedVacancyModel
from .base import BaseRepository


class SkippedVacanciesRepository(BaseRepository):
    __table__ = "skipped_vacancies"
    model = SkippedVacancyModel
    conflict_columns = ("resume_id", "vacancy_id")

    def is_skipped(self, resume_id: str, vacancy_id: str) -> bool:
        """Return True if this resume+vacancy pair was already skipped."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM skipped_vacancies WHERE resume_id = ? AND vacancy_id = ?",
            (resume_id, vacancy_id),
        )
        return cur.fetchone()[0] > 0

    def save(self, data: SkippedVacancyModel | dict[str, Any], /, **kwargs: Any) -> None:
        """Insert a skipped vacancy record. Uses INSERT OR IGNORE to never overwrite."""
        if isinstance(data, dict):
            data = self.model.from_api(data)
        row = data.to_db()
        columns = list(row.keys())
        self.conn.execute(
            f"INSERT OR IGNORE INTO {self.table_name} ({', '.join(columns)})"
            f" VALUES (:{', :'.join(columns)})",
            row,
        )
        self.maybe_commit(kwargs.get("commit"))

    def clear(self, reason: str | None = None, /, commit: bool | None = None) -> int:
        """Delete all skipped vacancies, or only those with a matching reason.
        Returns the number of deleted rows."""
        if reason is not None:
            cur = self.conn.execute(
                "DELETE FROM skipped_vacancies WHERE reason = ?",
                (reason,),
            )
        else:
            cur = self.conn.execute("DELETE FROM skipped_vacancies")
        self.maybe_commit(commit)
        return cur.rowcount if cur.rowcount != -1 else 0
