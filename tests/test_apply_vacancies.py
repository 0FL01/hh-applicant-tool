import sqlite3
import sys
import types
from types import SimpleNamespace

openai_module = types.ModuleType("openai")
openai_module.OpenAI = object
sys.modules.setdefault("openai", openai_module)

from hh_applicant_tool.operations.apply_vacancies import Operation
from hh_applicant_tool.storage import StorageFacade


RESUME = {
    "id": "resume-1",
    "title": "Backend Engineer",
    "alternate_url": "https://hh.ru/resume/resume-1",
}

USER = {
    "first_name": "Ivan",
    "last_name": "Petrov",
    "email": "ivan@example.com",
    "phone": "+79990000000",
}


class FakeApiClient:
    def __init__(self, descriptions):
        self.descriptions = descriptions
        self.post_calls = []

    def get(self, path, *args, **kwargs):
        if path.startswith("/vacancies/"):
            vacancy_id = path.rsplit("/", 1)[-1]
            return {"description": self.descriptions[vacancy_id]}
        raise AssertionError(f"Unexpected GET path: {path}")

    def post(self, path, params, delay=None):
        assert path == "/negotiations"
        self.post_calls.append({"params": params, "delay": delay})
        return {}


def make_vacancy(
    vacancy_id,
    *,
    employer_id="501",
    name="Backend Engineer",
    area_id="1",
    area_name="Москва",
    relations=None,
):
    return {
        "id": vacancy_id,
        "premium": False,
        "name": name,
        "department": None,
        "has_test": False,
        "response_letter_required": False,
        "area": {"id": area_id, "name": area_name},
        "salary": None,
        "salary_range": None,
        "type": {"id": "open", "name": "Открытая"},
        "address": None,
        "response_url": None,
        "sort_point_distance": None,
        "published_at": "2026-03-18T12:00:00+03:00",
        "created_at": "2026-03-18T12:00:00+03:00",
        "archived": False,
        "apply_alternate_url": f"https://hh.ru/applicant/vacancy_response?vacancyId={vacancy_id}",
        "show_contacts": False,
        "benefits": [],
        "insider_interview": None,
        "url": f"https://api.hh.ru/vacancies/{vacancy_id}",
        "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}",
        "professional_roles": [],
        "employer": {
            "id": employer_id,
            "name": "Acme",
            "url": f"https://api.hh.ru/employers/{employer_id}",
            "alternate_url": f"https://hh.ru/employer/{employer_id}",
            "logo_urls": None,
            "vacancies_url": f"https://api.hh.ru/vacancies?employer_id={employer_id}",
            "accredited_it_employer": False,
            "trusted": False,
        },
        "relations": list(relations or []),
        "experimental_modes": [],
        "manager_activity": None,
        "snippet": {
            "requirement": "Python",
            "responsibility": "Build APIs",
        },
        "contacts": {},
        "schedule": {"id": "remote", "name": "Удаленно"},
        "working_days": [],
        "working_time_intervals": [],
        "working_time_modes": [],
        "accept_temporary": False,
        "fly_in_fly_out_duration": [],
        "work_format": [],
        "working_hours": [],
        "work_schedule_by_days": [],
        "accept_labor_contract": False,
        "civil_law_contracts": [],
        "night_shifts": False,
        "accept_incomplete_resumes": False,
        "experience": {"id": "between1And3", "name": "1-3 years"},
        "employment": {"id": "full", "name": "Полная занятость"},
        "employment_form": {"id": "full", "name": "Полная занятость"},
        "internship": False,
        "adv_response_url": None,
        "is_adv_vacancy": False,
        "adv_context": None,
        "allow_chat_with_manager": False,
    }


def make_operation(vacancies, descriptions, *, dry_run=False):
    storage = StorageFacade(sqlite3.connect(":memory:"))
    api_client = FakeApiClient(descriptions)
    tool = SimpleNamespace(
        storage=storage,
        api_client=api_client,
        args=SimpleNamespace(send_email=False),
        config={},
    )
    operation = Operation()
    operation.tool = tool
    operation.cover_letter = ""
    operation.dedupe_vacancies = True
    operation.dry_run = dry_run
    operation.excluded_filter = None
    operation.excluded_keywords_filter = None
    operation.force_message = False
    operation.max_responses = None
    operation.openai_chat = None
    operation.pre_prompt = ""
    operation._vacancy_description_cache = {}
    operation._get_vacancies = lambda resume_id=None: iter(vacancies)
    return operation, tool, api_client


def test_skips_duplicate_vacancies_by_title_and_description(caplog):
    first = make_vacancy("101", area_id="1", area_name="Москва")
    second = make_vacancy("202", area_id="2", area_name="Санкт-Петербург")
    operation, tool, api_client = make_operation(
        [first, second],
        {
            "101": "<p>Build APIs for our platform</p>",
            "202": "<div>Build APIs for our platform</div>",
        },
    )

    caplog.set_level("INFO", logger="hh_applicant_tool.operations")

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert len(api_client.post_calls) == 1
    assert api_client.post_calls[0]["params"]["vacancy_id"] == "101"
    assert tool.storage.vacancy_response_dedup.count_total() == 1
    assert any(
        "Пропускаем дубликат вакансии" in record.getMessage()
        for record in caplog.records
    )


def test_existing_relations_seed_dedupe_before_apply():
    fresh_duplicate = make_vacancy("101")
    responded_duplicate = make_vacancy("202", relations=["response"])
    operation, tool, api_client = make_operation(
        [fresh_duplicate, responded_duplicate],
        {
            "101": "<p>Same role description</p>",
            "202": "<div>Same role description</div>",
        },
    )

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert api_client.post_calls == []
    assert tool.storage.vacancy_response_dedup.count_total() == 1


def test_dry_run_does_not_persist_dedupe_records():
    first = make_vacancy("101")
    second = make_vacancy("202")
    operation, tool, api_client = make_operation(
        [first, second],
        {
            "101": "<p>Dry run description</p>",
            "202": "<div>Dry run description</div>",
        },
        dry_run=True,
    )

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert api_client.post_calls == []
    assert tool.storage.vacancy_response_dedup.count_total() == 0
