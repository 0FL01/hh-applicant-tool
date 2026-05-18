from __future__ import annotations

from ..models.vacancy_analysis import VacancyAnalysisModel
from .base import BaseRepository


class VacancyAnalysisRepository(BaseRepository):
    __table__ = "vacancy_analysis"
    model = VacancyAnalysisModel

    def latest_for_vacancy(
        self,
        *,
        resume_id: str,
        vacancy_id: int | str,
        policy_hash: str | None = None,
    ) -> VacancyAnalysisModel | None:
        params: dict[str, object] = {
            "resume_id": resume_id,
            "vacancy_id": int(vacancy_id),
        }
        where = "resume_id = :resume_id AND vacancy_id = :vacancy_id"
        if policy_hash is not None:
            where += " AND policy_hash = :policy_hash"
            params["policy_hash"] = policy_hash
        cur = self.conn.execute(
            f"SELECT * FROM {self.table_name} WHERE {where} ORDER BY created_at DESC LIMIT 1",
            params,
        )
        row = cur.fetchone()
        return self._row_to_model(cur, row) if row else None
