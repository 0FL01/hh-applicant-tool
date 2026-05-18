from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import anyio

from hh_applicant_tool.mcp.context import MCPRuntime, MCPServerConfig
from hh_applicant_tool.mcp.server import build_server
from hh_applicant_tool.mcp.tools import MCPToolHandlers
from hh_applicant_tool.storage import StorageFacade


class FakeProfile:
    profile_id = "profile-a"

    def __init__(self):
        self.storage = StorageFacade(sqlite3.connect(":memory:"))
        self.flushed = 0

    def get_me(self):
        return {
            "id": 42,
            "first_name": "Ivan",
            "last_name": "Petrov",
            "middle_name": None,
            "email": "ivan@example.com",
            "phone": "+79990000000",
            "counters": {"resumes": 1},
        }

    def get_resumes(self):
        return [
            {
                "id": "resume-1",
                "title": "Backend Engineer",
                "url": "https://api.hh.ru/resumes/resume-1",
                "alternate_url": "https://hh.ru/resume/resume-1",
                "status": {"id": "published", "name": "published"},
                "can_publish_or_update": False,
                "counters": {"total_views": 1, "new_views": 0},
            }
        ]

    def save_token(self):
        self.flushed += 1
        return False

    def save_cookies(self):
        self.flushed += 1


class FakeService:
    def __init__(self):
        self.apply_calls = []

    def apply_vacancy(self, **kwargs):
        self.apply_calls.append(kwargs)
        return SimpleNamespace(
            attempt_id="attempt-1",
            status="blocked",
            reason="server_apply_disabled",
            analysis_id="analysis-1",
            dry_run=kwargs["dry_run"],
            cover_letter_source="none",
            cover_letter_preview=None,
            policy_hash="policy-hash",
            dedupe_key="dedupe",
            safety_blocks=["server_apply_disabled"],
            vacancy={"id": kwargs["vacancy_id"], "alternate_url": None},
        )


def make_runtime():
    return MCPRuntime(
        profile=FakeProfile(),
        service=FakeService(),
        config=MCPServerConfig(allow_apply=False),
    )


def test_whoami_persists_mcp_run():
    runtime = make_runtime()
    handlers = MCPToolHandlers(runtime)

    result = handlers.hh_whoami()

    assert result["authenticated"] is True
    assert result["profile_id"] == "profile-a"
    run = runtime.profile.storage.mcp_runs.get(result["run_id"])
    assert run.status == "completed"
    assert run.tool_name == "hh_whoami"


def test_apply_tool_passes_fail_closed_server_gate():
    runtime = make_runtime()
    handlers = MCPToolHandlers(runtime)

    result = handlers.hh_apply_vacancy(
        resume_id="resume-1",
        vacancy_id="101",
        dry_run=False,
        confirm_apply=True,
    )

    assert result["status"] == "blocked"
    assert runtime.service.apply_calls[0]["allow_apply"] is False
    assert runtime.service.apply_calls[0]["dry_run"] is False
    assert runtime.service.apply_calls[0]["confirm_apply"] is True


def test_build_server_registers_mvp_tools():
    async def run():
        app = build_server(make_runtime())
        tools = await app.list_tools()
        names = {tool.name for tool in tools}
        assert {
            "hh_whoami",
            "hh_list_resumes",
            "hh_search_vacancies",
            "hh_get_vacancy",
            "hh_analyze_vacancy",
            "hh_research_vacancies",
            "hh_apply_vacancy",
            "hh_research_and_apply",
        }.issubset(names)

    anyio.run(run)
