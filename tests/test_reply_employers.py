from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from hh_applicant_tool.operations.reply_employers import Operation
from hh_applicant_tool.storage import StorageFacade


RESUME = {
    "id": "resume-1",
    "title": "Backend Engineer",
    "status": {"id": "published"},
}

USER = {"first_name": "Ivan", "last_name": "Petrov"}


def make_negotiation(*, resume=RESUME, drop_resume=False):
    negotiation = {
        "id": 1,
        "state": {"id": "active"},
        "updated_at": "2026-09-10T10:00:00+03:00",
        "viewed_by_opponent": False,
        "resume": resume,
        "vacancy": {
            "id": 101,
            "name": "Platform Engineer",
            "alternate_url": "https://hh.ru/vacancy/101",
            "employer": {"id": 501, "name": "Acme"},
            "salary": None,
        },
    }
    if drop_resume:
        negotiation.pop("resume")
    return negotiation


def make_message(text, participant="employer"):
    return {
        "author": {"participant_type": participant},
        "text": text,
        "created_at": "2026-09-10T09:00:00+03:00",
    }


class FakeApiClient:
    def __init__(self, pages):
        self.pages = pages
        self.get_calls = []

    def get(self, path, page=0, **kwargs):
        self.get_calls.append((path, page))
        if path.endswith("/messages"):
            return self.pages[min(page, len(self.pages) - 1)]
        raise AssertionError(f"Unexpected GET path: {path}")

    def post(self, path, *args, **kwargs):
        raise AssertionError("post must not be called in these tests")


def make_operation(negotiations, api_client):
    tool = SimpleNamespace(
        storage=StorageFacade(sqlite3.connect(":memory:")),
        get_negotiations=lambda: iter(negotiations),
    )
    operation = Operation()
    operation.tool = tool
    operation.api_client = api_client
    operation.period = None
    operation.only_invitations = False
    operation.reply_message = "Hello"
    operation.dry_run = True
    operation.openai_chat = None
    operation.pre_prompt = ""
    return operation


def test_null_resume_negotiation_is_skipped_without_crash():
    api_client = FakeApiClient(
        [{"items": [make_message("hi")], "pages": 1, "per_page": 100}]
    )
    operation = make_operation(
        [make_negotiation(resume=None)], api_client
    )

    operation._reply_chats(user=USER, resumes=[RESUME], blacklist=set())

    assert api_client.get_calls == []


def test_missing_resume_key_negotiation_is_skipped_without_crash():
    api_client = FakeApiClient(
        [{"items": [make_message("hi")], "pages": 1, "per_page": 100}]
    )
    operation = make_operation(
        [make_negotiation(drop_resume=True)], api_client
    )

    operation._reply_chats(user=USER, resumes=[RESUME], blacklist=set())

    assert api_client.get_calls == []


def test_message_pages_are_iterated_sequentially():
    pages = [
        {
            "items": [make_message("first")],
            "pages": 3,
            "page": 0,
            "per_page": 1,
        },
        {
            "items": [make_message("second")],
            "pages": 3,
            "page": 1,
            "per_page": 1,
        },
        {
            "items": [make_message("third")],
            "pages": 3,
            "page": 2,
            "per_page": 1,
        },
    ]
    api_client = FakeApiClient(pages)
    operation = make_operation([make_negotiation()], api_client)

    operation._reply_chats(user=USER, resumes=[RESUME], blacklist=set())

    message_pages = [
        page for path, page in api_client.get_calls if "messages" in path
    ]
    # Раньше был прыжок page = pages - 1: страницы 1 не существовало бы.
    assert message_pages == [0, 1, 2]
