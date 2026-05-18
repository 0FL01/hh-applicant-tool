from __future__ import annotations

from typing import Any

from ..models.vacancy_response_dedup import VacancyResponseDedupModel
from .base import BaseRepository


class VacancyResponseDedupRepository(BaseRepository):
    __table__ = "vacancy_response_dedup"
    model = VacancyResponseDedupModel
    conflict_columns = ("resume_id", "dedupe_key")

    def exists(self, *, resume_id: str, dedupe_key: str) -> bool:
        cur = self.conn.execute(
            f"SELECT 1 FROM {self.table_name} WHERE resume_id = ? AND dedupe_key = ?",
            (resume_id, dedupe_key),
        )
        return cur.fetchone() is not None

    def list_vacancy_ids_by_key(self, resume_id: str) -> dict[str, int]:
        cur = self.conn.execute(
            f"SELECT dedupe_key, vacancy_id FROM {self.table_name} WHERE resume_id = ?",
            (resume_id,),
        )
        return {
            str(dedupe_key): int(vacancy_id)
            for dedupe_key, vacancy_id in cur.fetchall()
        }

    def remember(
        self,
        *,
        resume_id: str,
        dedupe_key: str,
        vacancy_id: int | str,
        vacancy_name: str,
        employer_id: int | str | None = None,
        alternate_url: str | None = None,
        commit: bool | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "resume_id": resume_id,
            "dedupe_key": dedupe_key,
            "vacancy_id": vacancy_id,
            "vacancy_name": vacancy_name,
            "alternate_url": alternate_url,
        }
        if employer_id is not None:
            data["employer_id"] = employer_id
        self._insert(
            data, conflict_columns=self.conflict_columns, commit=commit
        )
