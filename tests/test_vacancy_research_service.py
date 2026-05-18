from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

from hh_applicant_tool.services import (
    SearchFilters,
    VacancyPolicy,
    VacancyResearchService,
)
from hh_applicant_tool.storage import StorageFacade


class FakeApiClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, endpoint, params=None, **kwargs):
        self.calls.append((endpoint, params, kwargs))
        key = (endpoint, len([c for c in self.calls if c[0] == endpoint]) - 1)
        if key in self.responses:
            return self.responses[key]
        if endpoint in self.responses:
            return self.responses[endpoint]
        raise AssertionError(f"Unexpected endpoint: {endpoint}")


def make_storage():
    return StorageFacade(sqlite3.connect(":memory:"))


def make_context(api_client, storage):
    return SimpleNamespace(api_client=api_client, storage=storage)


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
