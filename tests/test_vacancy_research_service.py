from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

from hh_applicant_tool.services import (
    CoverLetterRequest,
    SearchFilters,
    VacancyPolicy,
    VacancyResearchService,
)
from hh_applicant_tool.storage import StorageFacade


class FakeApiClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.post_calls = []

    def get(self, endpoint, params=None, **kwargs):
        self.calls.append((endpoint, params, kwargs))
        key = (endpoint, len([c for c in self.calls if c[0] == endpoint]) - 1)
        if key in self.responses:
            return self.responses[key]
        if endpoint in self.responses:
            return self.responses[endpoint]
        raise AssertionError(f"Unexpected endpoint: {endpoint}")

    def post(self, endpoint, params=None, **kwargs):
        self.post_calls.append((endpoint, params, kwargs))
        return {}


class FakeLLMReply:
    def __init__(self, parsed, content='{"ok": true}'):
        self.parsed = parsed
        self.content = content
        self.reasoning_details = [{"step": "fake"}]


class FakeLLMClient:
    def __init__(self, *, parsed=None, error=None, message="Generated letter"):
        self.parsed = parsed
        self.error = error
        self.message = message
        self.complete_json_calls = []
        self.send_message_calls = []
        self.config = SimpleNamespace(model="fake-model")

    def complete_json(self, messages, *, schema):
        self.complete_json_calls.append((messages, schema))
        if self.error:
            raise self.error
        return FakeLLMReply(self.parsed)

    def send_message(self, message, system_prompt=None):
        self.send_message_calls.append((message, system_prompt))
        return self.message


def make_storage():
    return StorageFacade(sqlite3.connect(":memory:"))


def make_context(api_client, storage):
    return SimpleNamespace(
        api_client=api_client,
        storage=storage,
        get_resumes=lambda: [{"id": "resume-1", "title": "Backend Engineer"}],
    )


def make_vacancy(
    vacancy_id="101",
    *,
    name="Backend Engineer",
    employer_id="501",
    employer_name="Acme",
    work_format=None,
    **overrides,
):
    vacancy = {
        "id": vacancy_id,
        "name": name,
        "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}",
        "area": {"id": "1", "name": "Москва"},
        "salary": None,
        "schedule": {"id": "remote", "name": "Удаленно"},
        "experience": {"id": "between1And3", "name": "1-3 years"},
        "professional_roles": [],
        "employer": {
            "id": employer_id,
            "name": employer_name,
        },
        "snippet": {
            "requirement": "Python",
            "responsibility": "Build APIs",
        },
        "archived": False,
        "response_url": None,
        "adv_response_url": None,
        "has_test": False,
        "relations": [],
        "work_format": work_format or [],
    }
    vacancy.update(overrides)
    return vacancy


def test_search_vacancies_uses_api_params_and_updates_cache():
    vacancy = make_vacancy()
    api_client = FakeApiClient(
        {
            ("/vacancies", 0): {
                "found": 1,
                "pages": 1,
                "items": [vacancy],
            }
        }
    )
    storage = make_storage()
    service = VacancyResearchService(make_context(api_client, storage))

    result = service.search_vacancies(
        search="python",
        filters=SearchFilters(area=["1"], work_format=["REMOTE"]),
    )

    assert result.source == "search"
    assert result.items == [vacancy]
    assert result.total_found == 1
    assert api_client.calls[0][0] == "/vacancies"
    assert api_client.calls[0][1]["text"] == "python"
    assert api_client.calls[0][1]["area"] == ["1"]
    assert api_client.calls[0][1]["work_format"] == ["REMOTE"]
    assert storage.vacancies.count_total() == 1


def test_similar_vacancies_uses_resume_endpoint():
    vacancy = make_vacancy()
    api_client = FakeApiClient(
        {
            ("/resumes/resume-1/similar_vacancies", 0): {
                "found": 1,
                "pages": 1,
                "items": [vacancy],
            }
        }
    )
    service = VacancyResearchService(make_context(api_client, make_storage()))

    result = service.get_similar_vacancies(resume_id="resume-1")

    assert result.source == "similar"
    assert result.items == [vacancy]
    assert api_client.calls[0][0] == "/resumes/resume-1/similar_vacancies"


def test_dedupe_key_matches_existing_semantic_hash():
    vacancy = make_vacancy()
    api_client = FakeApiClient(
        {
            "/vacancies/101": {
                **vacancy,
                "description": "<p>Build&nbsp; APIs</p>",
            }
        }
    )
    service = VacancyResearchService(make_context(api_client, make_storage()))

    dedupe_key = service.build_vacancy_dedupe_key(vacancy)

    expected_payload = "501\nbackend engineer\nbuild apis"
    assert dedupe_key == hashlib.sha256(
        expected_payload.encode("utf-8")
    ).hexdigest()


def test_hard_prechecks_return_reasons_and_dedupe_key():
    vacancy = make_vacancy(
        name="Senior Backend Engineer",
        response_url="https://example.com/apply",
        has_test=True,
        work_format=[{"id": "OFFICE"}],
    )
    api_client = FakeApiClient(
        {
            "/vacancies/101": {
                **vacancy,
                "description": "<p>Build APIs</p>",
            }
        }
    )
    storage = make_storage()
    service = VacancyResearchService(make_context(api_client, storage))
    dedupe_key = service.build_vacancy_dedupe_key(vacancy)
    storage.vacancy_response_dedup.remember(
        resume_id="resume-1",
        dedupe_key=dedupe_key,
        vacancy_id="101",
        vacancy_name=vacancy["name"],
    )

    result = service.run_hard_prechecks(
        resume_id="resume-1",
        vacancy=vacancy,
        policy=VacancyPolicy(
            excluded_employers=["Acme"],
            excluded_keywords=["senior"],
        ),
        work_format=["REMOTE"],
    )

    assert result.blocked is True
    assert result.dedupe_key == dedupe_key
    assert result.reasons == [
        "manual_form_required",
        "has_test",
        "excluded_employer",
        "excluded_keyword",
        "work_format_mismatch",
        "dedupe_hit",
    ]


def test_service_records_precheck_analysis_audit_row():
    vacancy = make_vacancy(has_test=True)
    api_client = FakeApiClient(
        {
            "/vacancies/101": {
                **vacancy,
                "description": "<p>Build APIs</p>",
            }
        }
    )
    storage = make_storage()
    service = VacancyResearchService(make_context(api_client, storage))
    policy = VacancyPolicy(excluded_keywords=["senior"])
    precheck = service.run_hard_prechecks(
        resume_id="resume-1",
        vacancy=vacancy,
        policy=policy,
    )

    analysis = service.record_precheck_analysis(
        resume_id="resume-1",
        vacancy=vacancy,
        precheck=precheck,
        policy=policy,
        run_id="run-1",
        source="search",
        source_query="python",
    )

    stored = storage.vacancy_analysis.get(analysis.id)
    assert stored is not None
    assert stored.run_id == "run-1"
    assert stored.analysis_status == "blocked"
    assert stored.recommended_action == "skip"
    assert stored.policy_hash == policy.hash()
    assert stored.policy_json["excluded_keywords"] == ["senior"]
    assert stored.precheck_reasons_json == ["has_test"]
    assert stored.vacancy_snapshot_json["id"] == "101"


def test_analyze_vacancy_uses_structured_llm_and_persists_audit():
    vacancy = make_vacancy()
    api_client = FakeApiClient(
        {
            "/vacancies/101": {
                **vacancy,
                "description": "<p>Build APIs</p>",
            }
        }
    )
    storage = make_storage()
    llm = FakeLLMClient(
        parsed={
            "suitable": True,
            "score": 0.9,
            "reason": "Good backend match",
            "red_flags": [],
            "missing": [],
            "recommended_action": "apply",
        }
    )
    service = VacancyResearchService(
        make_context(api_client, storage),
        llm_client=llm,
    )

    result = service.analyze_vacancy(
        resume_id="resume-1",
        vacancy_id="101",
        policy=VacancyPolicy(min_score=0.7),
    )

    assert result.analysis_status == "ok"
    assert result.recommended_action == "apply"
    assert result.score == 0.9
    assert result.model == "fake-model"
    assert len(llm.complete_json_calls) == 1
    stored = storage.vacancy_analysis.get(result.analysis_id)
    assert stored.raw_response == '{"ok": true}'
    assert stored.reasoning_details == [{"step": "fake"}]


def test_analyze_vacancy_degrades_when_llm_is_unavailable():
    vacancy = make_vacancy()
    api_client = FakeApiClient(
        {
            "/vacancies/101": {
                **vacancy,
                "description": "<p>Build APIs</p>",
            }
        }
    )
    service = VacancyResearchService(make_context(api_client, make_storage()))

    result = service.analyze_vacancy(
        resume_id="resume-1",
        vacancy_id="101",
    )

    assert result.analysis_status == "degraded"
    assert result.recommended_action == "review"
    assert result.suitable is False


def test_apply_vacancy_dry_run_persists_planned_attempt_without_dedupe():
    vacancy = make_vacancy()
    api_client = FakeApiClient(
        {
            "/vacancies/101": {
                **vacancy,
                "description": "<p>Build APIs</p>",
            }
        }
    )
    storage = make_storage()
    llm = FakeLLMClient(
        parsed={
            "suitable": True,
            "score": 0.95,
            "reason": "Strong fit",
            "red_flags": [],
            "missing": [],
            "recommended_action": "apply",
        }
    )
    service = VacancyResearchService(
        make_context(api_client, storage),
        llm_client=llm,
    )

    result = service.apply_vacancy(
        resume_id="resume-1",
        vacancy_id="101",
        cover_letter_request=CoverLetterRequest(text="Hello"),
        dry_run=True,
        day_bucket="2026-05-18",
    )

    assert result.status == "planned"
    assert api_client.post_calls == []
    assert storage.application_attempts.count_total() == 1
    assert storage.vacancy_response_dedup.count_total() == 0


def test_apply_vacancy_real_apply_requires_allow_and_confirm():
    vacancy = make_vacancy()
    api_client = FakeApiClient(
        {
            "/vacancies/101": {
                **vacancy,
                "description": "<p>Build APIs</p>",
            }
        }
    )
    llm = FakeLLMClient(
        parsed={
            "suitable": True,
            "score": 0.95,
            "reason": "Strong fit",
            "red_flags": [],
            "missing": [],
            "recommended_action": "apply",
        }
    )
    service = VacancyResearchService(
        make_context(api_client, make_storage()),
        llm_client=llm,
    )

    result = service.apply_vacancy(
        resume_id="resume-1",
        vacancy_id="101",
        cover_letter_request=CoverLetterRequest(text="Hello"),
        dry_run=False,
        confirm_apply=False,
        allow_apply=False,
        day_bucket="2026-05-18",
    )

    assert result.status == "blocked"
    assert result.safety_blocks == [
        "server_apply_disabled",
        "confirm_apply_required",
    ]
    assert api_client.post_calls == []


def test_apply_vacancy_real_apply_posts_and_persists_dedupe():
    vacancy = make_vacancy()
    api_client = FakeApiClient(
        {
            "/vacancies/101": {
                **vacancy,
                "description": "<p>Build APIs</p>",
            }
        }
    )
    storage = make_storage()
    llm = FakeLLMClient(
        parsed={
            "suitable": True,
            "score": 0.95,
            "reason": "Strong fit",
            "red_flags": [],
            "missing": [],
            "recommended_action": "apply",
        }
    )
    service = VacancyResearchService(
        make_context(api_client, storage),
        llm_client=llm,
    )

    result = service.apply_vacancy(
        resume_id="resume-1",
        vacancy_id="101",
        cover_letter_request=CoverLetterRequest(text="Hello"),
        dry_run=False,
        confirm_apply=True,
        allow_apply=True,
        day_bucket="2026-05-18",
    )

    assert result.status == "applied"
    assert api_client.post_calls == [
        (
            "/negotiations",
            {"resume_id": "resume-1", "vacancy_id": "101", "message": "Hello"},
            {},
        )
    ]
    assert storage.application_attempts.count_applied_for_day("2026-05-18") == 1
    assert storage.vacancy_response_dedup.count_total() == 1


def test_apply_vacancy_blocks_stale_analysis_when_dedupe_exists():
    vacancy = make_vacancy()
    api_client = FakeApiClient(
        {
            "/vacancies/101": {
                **vacancy,
                "description": "<p>Build APIs</p>",
            }
        }
    )
    storage = make_storage()
    llm = FakeLLMClient(
        parsed={
            "suitable": True,
            "score": 0.95,
            "reason": "Strong fit",
            "red_flags": [],
            "missing": [],
            "recommended_action": "apply",
        }
    )
    service = VacancyResearchService(
        make_context(api_client, storage),
        llm_client=llm,
    )
    analysis = service.analyze_vacancy(
        resume_id="resume-1",
        vacancy_id="101",
    )
    storage.vacancy_response_dedup.remember(
        resume_id="resume-1",
        dedupe_key=analysis.dedupe_key,
        vacancy_id="100",
        vacancy_name="Previous Backend Engineer",
        employer_id="501",
    )

    result = service.apply_vacancy(
        resume_id="resume-1",
        vacancy_id="101",
        analysis_id=analysis.analysis_id,
        cover_letter_request=CoverLetterRequest(text="Hello"),
        dry_run=False,
        confirm_apply=True,
        allow_apply=True,
        day_bucket="2026-05-18",
    )

    assert result.status == "blocked"
    assert "dedupe_hit" in result.safety_blocks
    assert api_client.post_calls == []
