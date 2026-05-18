from __future__ import annotations

import sqlite3

from hh_applicant_tool.storage import StorageFacade
from hh_applicant_tool.storage.models.application_attempt import (
    ApplicationAttemptModel,
)
from hh_applicant_tool.storage.models.mcp_run import MCPRunModel
from hh_applicant_tool.storage.models.vacancy_analysis import (
    VacancyAnalysisModel,
)


def make_storage():
    return StorageFacade(sqlite3.connect(":memory:"))


def test_mcp_run_persists_json_and_finish_counts():
    storage = make_storage()

    storage.mcp_runs.save(
        MCPRunModel(
            id="run-1",
            tool_name="hh_research_vacancies",
            status="running",
            profile_id="profile-a",
            resume_id="resume-1",
            dry_run=True,
            confirm_apply=False,
            policy_hash="policy-hash",
            policy_json={"min_score": 0.7},
            search_params_json={"text": "python"},
        )
    )
    storage.mcp_runs.finish(
        "run-1",
        status="completed",
        total_candidates=3,
        analyzed_count=2,
        skipped_count=1,
    )

    row = storage.mcp_runs.get("run-1")

    assert row is not None
    assert row.status == "completed"
    assert row.policy_json == {"min_score": 0.7}
    assert row.search_params_json == {"text": "python"}
    assert row.total_candidates == 3
    assert row.analyzed_count == 2
    assert row.skipped_count == 1
    assert row.finished_at is not None


def test_vacancy_analysis_persists_structured_audit_fields():
    storage = make_storage()

    storage.vacancy_analysis.save(
        VacancyAnalysisModel(
            id="analysis-1",
            run_id="run-1",
            resume_id="resume-1",
            vacancy_id=101,
            employer_id=501,
            dedupe_key="dedupe",
            source="search",
            source_query="python",
            analysis_status="blocked",
            analysis_mode="light",
            policy_hash="policy-hash",
            policy_json={"excluded_keywords": ["senior"]},
            suitable=False,
            score=0.0,
            reason="Hard precheck blocked vacancy",
            red_flags_json=["manual_form_required"],
            missing_json=[],
            recommended_action="skip",
            precheck_reasons_json=["manual_form_required"],
            reasoning_details=[{"kind": "precheck"}],
            vacancy_snapshot_json={"id": "101", "name": "Backend"},
        )
    )

    row = storage.vacancy_analysis.latest_for_vacancy(
        resume_id="resume-1",
        vacancy_id=101,
        policy_hash="policy-hash",
    )

    assert row is not None
    assert row.policy_json == {"excluded_keywords": ["senior"]}
    assert row.red_flags_json == ["manual_form_required"]
    assert row.precheck_reasons_json == ["manual_form_required"]
    assert row.reasoning_details == [{"kind": "precheck"}]
    assert row.vacancy_snapshot_json == {"id": "101", "name": "Backend"}


def test_application_attempts_count_real_applies_only():
    storage = make_storage()
    attempts = [
        ApplicationAttemptModel(
            id="planned",
            resume_id="resume-1",
            vacancy_id=101,
            day_bucket="2026-05-18",
            dry_run=True,
            confirm_apply=False,
            status="planned",
            reason="dry-run",
            cover_letter_source="none",
        ),
        ApplicationAttemptModel(
            id="applied",
            resume_id="resume-1",
            vacancy_id=102,
            day_bucket="2026-05-18",
            dry_run=False,
            confirm_apply=True,
            status="applied",
            reason="sent",
            cover_letter_source="none",
        ),
        ApplicationAttemptModel(
            id="failed",
            resume_id="resume-1",
            vacancy_id=103,
            day_bucket="2026-05-18",
            dry_run=False,
            confirm_apply=True,
            status="failed",
            reason="failed",
            cover_letter_source="none",
        ),
    ]
    for attempt in attempts:
        storage.application_attempts.save(attempt)

    assert storage.application_attempts.count_applied_for_day("2026-05-18") == 1


def test_application_attempts_find_applied_or_unknown_blockers():
    storage = make_storage()
    storage.application_attempts.save(
        ApplicationAttemptModel(
            id="dry-run",
            resume_id="resume-1",
            vacancy_id=101,
            dedupe_key="dedupe",
            day_bucket="2026-05-18",
            dry_run=True,
            confirm_apply=False,
            status="planned",
            reason="dry-run",
            cover_letter_source="none",
        )
    )
    storage.application_attempts.save(
        ApplicationAttemptModel(
            id="unknown",
            resume_id="resume-1",
            vacancy_id=101,
            dedupe_key="dedupe",
            day_bucket="2026-05-18",
            dry_run=False,
            confirm_apply=True,
            status="unknown",
            reason="network timeout after POST",
            cover_letter_source="none",
        )
    )

    blocking = storage.application_attempts.latest_blocking_attempt(
        resume_id="resume-1",
        dedupe_key="dedupe",
    )

    assert blocking is not None
    assert blocking.id == "unknown"
