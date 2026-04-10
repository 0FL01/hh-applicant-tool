import sqlite3
import sys
import types
from types import SimpleNamespace
from pathlib import Path

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
        self.put_calls = []

    def get(self, path, *args, **kwargs):
        if path.startswith("/vacancies/"):
            vacancy_id = path.rsplit("/", 1)[-1]
            return {"description": self.descriptions[vacancy_id]}
        raise AssertionError(f"Unexpected GET path: {path}")

    def post(self, path, params, delay=None):
        assert path == "/negotiations"
        self.post_calls.append({"params": params, "delay": delay})
        return {}

    def put(self, path, *args, **kwargs):
        self.put_calls.append(path)
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
    operation.skip_tests = False
    operation.work_format = ["REMOTE"]
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


def test_env_excluded_keywords_skip_matching_vacancy_name():
    vacancy = make_vacancy("101", name="Senior Backend Engineer")
    operation, tool, api_client = make_operation(
        [vacancy],
        {"101": "<p>Build APIs for our platform</p>"},
    )
    operation.excluded_keywords_filter = r"(?:Senior)"
    operation.dedupe_vacancies = False

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert api_client.post_calls == []
    assert api_client.put_calls == ["/vacancies/blacklisted/101"]


def test_letter_file_short_alias_l_is_supported():
    import argparse

    parser = argparse.ArgumentParser()
    Operation().setup_parser(parser)

    args = parser.parse_args(["-l", "/app/letter.txt"])

    assert args.letter_file == Path("/app/letter.txt")


def test_env_excluded_keywords_do_not_skip_non_matching_vacancy_name():
    vacancy = make_vacancy("101", name="Backend Engineer")
    operation, tool, api_client = make_operation(
        [vacancy],
        {"101": "<p>Build APIs for our platform</p>"},
    )
    operation.excluded_keywords_filter = r"(?:Senior)"
    operation.dedupe_vacancies = False

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert len(api_client.post_calls) == 1
    assert api_client.post_calls[0]["params"]["vacancy_id"] == "101"


def test_env_excluded_keywords_skip_matching_employer_name():
    """Ключевое слово совпадает с названием компании, но не с названием вакансии
    — вакансия должна быть отфильтрована и добавлена в черный список."""
    vacancy = make_vacancy("202", name="Backend Engineer", employer_id="900")
    # Подменяем название работодателя на "Сбер", чтобы проверить фильтр по компании
    vacancy["employer"]["name"] = "Сбер"
    operation, tool, api_client = make_operation(
        [vacancy],
        {"202": "<p>Build APIs for our platform</p>"},
    )
    operation.excluded_keywords_filter = r"(?:Сбер)"
    operation.dedupe_vacancies = False

    operation._apply_resume(RESUME, USER, seen_employers={"900"})

    assert api_client.post_calls == []
    assert api_client.put_calls == ["/vacancies/blacklisted/202"]


def test_env_excluded_keywords_do_not_skip_when_employer_does_not_match():
    """Ключевое слово не совпадает ни с вакансией, ни с компанией — отклик проходит."""
    vacancy = make_vacancy("303", name="Backend Engineer", employer_id="800")
    vacancy["employer"]["name"] = "Yandex"
    operation, tool, api_client = make_operation(
        [vacancy],
        {"303": "<p>Build APIs for our platform</p>"},
    )
    operation.excluded_keywords_filter = r"(?:Сбер)"
    operation.dedupe_vacancies = False

    operation._apply_resume(RESUME, USER, seen_employers={"800"})

    assert len(api_client.post_calls) == 1
    assert api_client.post_calls[0]["params"]["vacancy_id"] == "303"


def test_work_format_filter_removes_on_site():
    """Вакансия с ON_SITE фильтруется при work_format=["REMOTE"]."""
    vacancy = make_vacancy("101")
    vacancy["work_format"] = [
        {"id": "ON_SITE", "name": "На месте работодателя"}
    ]
    operation, tool, api_client = make_operation(
        [vacancy],
        {"101": "<p>Build APIs for our platform</p>"},
    )
    operation.dedupe_vacancies = False

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert api_client.post_calls == []


def test_work_format_filter_passes_remote():
    """Вакансия с REMOTE проходит при work_format=["REMOTE"]."""
    vacancy = make_vacancy("101")
    vacancy["work_format"] = [{"id": "REMOTE", "name": "Удалённо"}]
    operation, tool, api_client = make_operation(
        [vacancy],
        {"101": "<p>Build APIs for our platform</p>"},
    )
    operation.dedupe_vacancies = False

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert len(api_client.post_calls) == 1
    assert api_client.post_calls[0]["params"]["vacancy_id"] == "101"


def test_work_format_filter_disabled_when_empty():
    """Пустой work_format=[] пропускает все вакансии."""
    vacancy = make_vacancy("101")
    vacancy["work_format"] = [
        {"id": "ON_SITE", "name": "На месте работодателя"}
    ]
    operation, tool, api_client = make_operation(
        [vacancy],
        {"101": "<p>Build APIs for our platform</p>"},
    )
    operation.dedupe_vacancies = False
    operation.work_format = []

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert len(api_client.post_calls) == 1
    assert api_client.post_calls[0]["params"]["vacancy_id"] == "101"


def test_work_format_filter_multiple():
    """work_format=["REMOTE","HYBRID"] пропускает REMOTE и HYBRID, отсекает ON_SITE."""
    remote = make_vacancy("101")
    remote["work_format"] = [{"id": "REMOTE", "name": "Удалённо"}]
    hybrid = make_vacancy("202")
    hybrid["work_format"] = [{"id": "HYBRID", "name": "Гибрид"}]
    onsite = make_vacancy("303")
    onsite["work_format"] = [{"id": "ON_SITE", "name": "На месте работодателя"}]
    operation, tool, api_client = make_operation(
        [remote, hybrid, onsite],
        {
            "101": "<p>Remote</p>",
            "202": "<p>Hybrid</p>",
            "303": "<p>On-site</p>",
        },
    )
    operation.dedupe_vacancies = False
    operation.work_format = ["REMOTE", "HYBRID"]

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    applied_ids = [c["params"]["vacancy_id"] for c in api_client.post_calls]
    assert applied_ids == ["101", "202"]


def test_work_format_filter_passes_when_vacancy_has_no_format():
    """Вакансия без work_format не фильтруется (поле пустое или отсутствует)."""
    vacancy = make_vacancy("101")
    vacancy["work_format"] = []
    operation, tool, api_client = make_operation(
        [vacancy],
        {"101": "<p>Build APIs for our platform</p>"},
    )
    operation.dedupe_vacancies = False

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert len(api_client.post_calls) == 1
    assert api_client.post_calls[0]["params"]["vacancy_id"] == "101"
