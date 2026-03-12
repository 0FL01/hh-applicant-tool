from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

QUERIES_PATH: Path = Path(__file__).parent / "queries"
MIGRATION_PATH: Path = QUERIES_PATH / "migrations"


logger: logging.Logger = logging.getLogger(__package__)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})")
    return {str(row[1]) for row in rows.fetchall()}


def _ensure_agent_decision_columns(conn: sqlite3.Connection) -> None:
    existing = _table_columns(conn, "agent_decisions")
    required_columns = {
        "classifier_category": "TEXT",
        "classifier_reason": "TEXT",
        "classifier_confidence": "REAL",
        "classifier_model": "TEXT",
        "classifier_raw_response": "TEXT",
    }
    for column_name, column_type in required_columns.items():
        if column_name in existing:
            continue
        conn.execute(
            f"ALTER TABLE agent_decisions ADD COLUMN {column_name} {column_type}"
        )
        logger.info("Добавлена колонка %s.%s", "agent_decisions", column_name)


def init_db(conn: sqlite3.Connection) -> None:
    """Создает схему БД"""
    changes_before = conn.total_changes

    conn.executescript(
        (QUERIES_PATH / "schema.sql").read_text(encoding="utf-8")
    )
    _ensure_agent_decision_columns(conn)

    if conn.total_changes > changes_before:
        logger.info("Применена схема бд")
    # else:
    #     logger.debug("База данных не изменилась.")


def list_migrations() -> list[str]:
    """Выводит имена миграций без расширения, отсортированные по дате"""
    if not MIGRATION_PATH.exists():
        return []
    return sorted([f.stem for f in MIGRATION_PATH.glob("*.sql")])


def apply_migration(conn: sqlite3.Connection, name: str) -> None:
    """Находит файл по имени и выполняет его содержимое"""
    conn.executescript(
        (MIGRATION_PATH / f"{name}.sql").read_text(encoding="utf-8")
    )


# def model2table(o: type) -> str:
#     name: str = o.__name__
#     if name.endswith("Model"):
#         name = name[:-5]
#     return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
