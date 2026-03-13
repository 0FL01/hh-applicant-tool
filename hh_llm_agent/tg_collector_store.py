from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StoredLead:
    idempotency_key: str
    event_type: str
    created_at: str
    negotiation_id: int | None
    last_message_id: str | None
    vacancy_name: str | None
    employer_name: str | None
    recruiter_name: str | None
    telegram_handles: list[str]
    emails: list[str]
    phones: list[str]
    payload_json: dict[str, Any]
    delivery_status: str
    delivery_attempts: int
    telegram_message_id: int | None
    last_error: str | None
    sent_at: str | None


class TelegramCollectorStore:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_contact_leads (
                    idempotency_key TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    negotiation_id INTEGER,
                    last_message_id TEXT,
                    vacancy_name TEXT,
                    employer_name TEXT,
                    recruiter_name TEXT,
                    telegram_handles_json TEXT NOT NULL DEFAULT '[]',
                    emails_json TEXT NOT NULL DEFAULT '[]',
                    phones_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'received',
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    telegram_message_id INTEGER,
                    last_error TEXT,
                    received_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    sent_at DATETIME
                );
                """
            )

    def get(self, idempotency_key: str) -> StoredLead | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM telegram_contact_leads WHERE idempotency_key = ?;",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_model(row)

    def save_event(self, record: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_contact_leads (
                    idempotency_key,
                    event_type,
                    created_at,
                    negotiation_id,
                    last_message_id,
                    vacancy_name,
                    employer_name,
                    recruiter_name,
                    telegram_handles_json,
                    emails_json,
                    phones_json,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    event_type = excluded.event_type,
                    created_at = excluded.created_at,
                    negotiation_id = excluded.negotiation_id,
                    last_message_id = excluded.last_message_id,
                    vacancy_name = excluded.vacancy_name,
                    employer_name = excluded.employer_name,
                    recruiter_name = excluded.recruiter_name,
                    telegram_handles_json = excluded.telegram_handles_json,
                    emails_json = excluded.emails_json,
                    phones_json = excluded.phones_json,
                    payload_json = excluded.payload_json;
                """,
                (
                    record["idempotency_key"],
                    record["event_type"],
                    record["created_at"],
                    record.get("negotiation_id"),
                    record.get("last_message_id"),
                    record.get("vacancy_name"),
                    record.get("employer_name"),
                    record.get("recruiter_name"),
                    json.dumps(record.get("telegram_handles") or []),
                    json.dumps(record.get("emails") or []),
                    json.dumps(record.get("phones") or []),
                    json.dumps(record["payload_json"]),
                ),
            )

    def mark_attempt(self, idempotency_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE telegram_contact_leads
                SET delivery_attempts = delivery_attempts + 1,
                    delivery_status = 'sending',
                    last_error = NULL
                WHERE idempotency_key = ?;
                """,
                (idempotency_key,),
            )

    def mark_sent(
        self,
        idempotency_key: str,
        *,
        telegram_message_id: int | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE telegram_contact_leads
                SET delivery_status = 'sent',
                    telegram_message_id = ?,
                    sent_at = CURRENT_TIMESTAMP,
                    last_error = NULL
                WHERE idempotency_key = ?;
                """,
                (telegram_message_id, idempotency_key),
            )

    def mark_failed(self, idempotency_key: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE telegram_contact_leads
                SET delivery_status = 'failed',
                    last_error = ?
                WHERE idempotency_key = ?;
                """,
                (error, idempotency_key),
            )

    def _row_to_model(self, row: sqlite3.Row) -> StoredLead:
        return StoredLead(
            idempotency_key=str(row["idempotency_key"]),
            event_type=str(row["event_type"]),
            created_at=str(row["created_at"]),
            negotiation_id=row["negotiation_id"],
            last_message_id=row["last_message_id"],
            vacancy_name=row["vacancy_name"],
            employer_name=row["employer_name"],
            recruiter_name=row["recruiter_name"],
            telegram_handles=json.loads(row["telegram_handles_json"] or "[]"),
            emails=json.loads(row["emails_json"] or "[]"),
            phones=json.loads(row["phones_json"] or "[]"),
            payload_json=json.loads(row["payload_json"] or "{}"),
            delivery_status=str(row["delivery_status"]),
            delivery_attempts=int(row["delivery_attempts"] or 0),
            telegram_message_id=row["telegram_message_id"],
            last_error=row["last_error"],
            sent_at=row["sent_at"],
        )
