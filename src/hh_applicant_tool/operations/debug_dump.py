"""Собрать сырые данные API из переговоров с роботом-рекрутером для анализа.

Обходит все активные переговоры, загружает полные ответы API
(без фильтрации with_text_only) и сохраняет JSON-дамп тех,
голько где встречается «Робот-рекрутер».
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from hh_applicant_tool.main import BaseNamespace, BaseOperation, HHApplicantTool

logger = logging.getLogger(__name__)

_ROBOT_MARKER = "робот-рекрутер"
_ROBOT_MARKER_ALT = "робот—рекрутер"


class Namespace(BaseNamespace):
    output: str | None
    all_negotiations: bool
    limit: int
    verbose: bool


class Operation(BaseOperation):
    """Собрать сырые данные API из переговоров с роботом-рекрутером."""

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "-o",
            "--output",
            help="Путь к файлу дампа (по умолчанию config/<profile>/debug_dump.json)",
            default=None,
        )
        parser.add_argument(
            "-a",
            "--all-negotiations",
            help="Дампить ВСЕ переговоры, не только с роботом-рекрутером",
            action="store_true",
            default=False,
        )
        parser.add_argument(
            "-n",
            "--limit",
            help="Максимум переговоров для обработки (0 = без лимита)",
            type=int,
            default=0,
        )
        parser.add_argument(
            "--verbose",
            help="Печатать полный JSON каждого сообщения в консоль",
            action="store_true",
            default=False,
        )

    def run(self, tool: HHApplicantTool) -> None | int:
        args: Namespace = tool.args
        api = tool.api_client

        output_path = (
            Path(args.output)
            if args.output
            else tool.config_path / "debug_dump.json"
        )

        print(f"[*] Загрузка переговоров...")
        negotiations = list(tool.get_negotiations())
        total = len(negotiations)
        print(f"[*] Всего переговоров: {total}")

        dumped: list[dict] = []
        robot_count = 0
        processed = 0

        for neg in negotiations:
            if args.limit and processed >= args.limit:
                print(f"[*] Достигнут лимит --limit={args.limit}")
                break
            processed += 1
            nid = neg.get("id", "?")
            neg_title = ""
            vacancy = neg.get("vacancy") or {}
            employer = (vacancy.get("employer") or {}).get("name", "")
            neg_title = f"{vacancy.get('name', '')} ({employer})"

            # Загружаем полные сообщения (без with_text_only)
            print(f"  [{processed}/{total}] nid={nid} {neg_title[:60]}")

            try:
                messages_raw = _fetch_all_messages(api, nid)
            except Exception as ex:
                logger.warning(
                    "Failed to fetch messages for nid=%s: %s", nid, ex
                )
                continue

            all_items = messages_raw.get("items", [])
            has_robot = any(_has_robot_marker(msg) for msg in all_items)

            if not has_robot and not args.all_negotiations:
                continue

            if has_robot:
                robot_count += 1

            entry = {
                "negotiation": neg,
                "messages_response": messages_raw,
                "dumped_at": datetime.now(timezone.utc).isoformat(),
            }
            dumped.append(entry)

            if has_robot:
                print(
                    f"    >>> РОБОТ-РЕКРУТЕР найден! ({len(all_items)} сообщений)"
                )
            elif args.all_negotiations:
                print(f"    >>> {len(all_items)} сообщений")

            if args.verbose:
                for msg in all_items:
                    print(json.dumps(msg, ensure_ascii=False, indent=2))

        print(
            f"\n[*] Обработано: {processed}, с роботом-рекрутером: {robot_count}"
        )
        print(f"[*] Запись дампа в {output_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(dumped, f, ensure_ascii=False, indent=2)

        print(f"[*] Дамп сохранён: {len(dumped)} переговоров, {output_path}")
        return 0


def _fetch_all_messages(api, nid: str) -> dict:
    """Загрузить все страницы сообщений без with_text_only."""
    all_items: list[dict] = []
    page = 0
    total_pages = 1
    while page < total_pages:
        response: dict = api.get(
            f"/negotiations/{nid}/messages",
            page=page,
        )
        items = response.get("items", [])
        if not items:
            break
        all_items.extend(items)
        total_pages = response.get("pages", 1)
        page += 1
    return {
        "items": all_items,
        "pages": total_pages,
        "total_collected": len(all_items),
    }


def _has_robot_marker(msg: dict) -> bool:
    text = (msg.get("text") or "").lower()
    return _ROBOT_MARKER in text or _ROBOT_MARKER_ALT in text
