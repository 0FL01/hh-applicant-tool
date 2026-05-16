from __future__ import annotations

import typing
from typing import Any

from ..models.skipped_vacancy import SkippedVacancyModel
from .base import BaseRepository


class SkippedVacanciesRepository(BaseRepository):
    __table__ = "skipped_vacancies"
    model = SkippedVacancyModel
    conflict_columns = ("resume_id", "vacancy_id")

    def is_skipped(self, resume_id: str, vacancy_id: str | int) -> bool:
        """Return True if this resume+vacancy pair was already skipped."""
        cur = self.conn.execute(
            "SELECT 1 FROM skipped_vacancies WHERE resume_id = ? AND vacancy_id = ?",
            (resume_id, int(vacancy_id)),
        )
        return cur.fetchone() is not None

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

    def remember(
        self,
        *,
        resume_id: str,
        vacancy_id: int | str,
        vacancy_name: str,
        employer_id: int | str | None = None,
        alternate_url: str | None = None,
        reason: str = "ai_rejected",
        resume_analysis_mode: str | None = None,
        commit: bool | None = None,
    ) -> None:
        data: dict[str, typing.Any] = {
            "resume_id": resume_id,
            "vacancy_id": int(vacancy_id),
            "vacancy_name": vacancy_name,
            "alternate_url": alternate_url,
            "reason": reason,
            "resume_analysis_mode": resume_analysis_mode,
        }
        if employer_id is not None:
            data["employer_id"] = int(employer_id)
        self._insert(data, conflict_columns=self.conflict_columns, commit=commit)

    def clear_for_resume(self, resume_id: str, commit: bool | None = None) -> None:
        self.conn.execute(
            f"DELETE FROM {self.table_name} WHERE resume_id = ?", (resume_id,)
        )
        self.maybe_commit(commit)
