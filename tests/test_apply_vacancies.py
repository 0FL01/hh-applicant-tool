import asyncio
import sqlite3
import sys
import types
from types import SimpleNamespace
from pathlib import Path

import pytest
from requests import Request, Response
from requests.cookies import RequestsCookieJar

openai_module = types.ModuleType("openai")
openai_module.OpenAI = object
sys.modules.setdefault("openai", openai_module)

from hh_applicant_tool.api.errors import CaptchaRequired
from hh_applicant_tool.operations.apply_vacancies import (
    CaptchaSolveError,
    Operation,
)
from hh_applicant_tool.operations.apply_vacancies import (
    validate_apply_delays,
)
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
    def __init__(self, descriptions, *, get_error=None, post_error=None):
        self.descriptions = descriptions
        self.get_error = get_error
        self.post_error = post_error
        self.post_calls = []
        self.put_calls = []

    def get(self, path, *args, **kwargs):
        if path.startswith("/vacancies/"):
            if self.get_error is not None:
                raise self.get_error
            vacancy_id = path.rsplit("/", 1)[-1]
            return {"description": self.descriptions[vacancy_id]}
        raise AssertionError(f"Unexpected GET path: {path}")

    def post(self, path, params, delay=None):
        assert path == "/negotiations"
        self.post_calls.append({"params": params, "delay": delay})
        if self.post_error is not None:
            raise self.post_error
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


def make_operation(
    vacancies,
    descriptions,
    *,
    dry_run=False,
    get_error=None,
    post_error=None,
):
    storage = StorageFacade(sqlite3.connect(":memory:"))
    api_client = FakeApiClient(
        descriptions,
        get_error=get_error,
        post_error=post_error,
    )
    tool = SimpleNamespace(
        storage=storage,
        api_client=api_client,
        args=SimpleNamespace(send_email=False),
        config={},
    )
    operation = Operation()
    operation.tool = tool
    operation.apply_delay_min = 0
    operation.apply_delay_max = 0
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
    operation._captcha_ai = None
    operation._get_vacancies = lambda resume_id=None: iter(vacancies)
    return operation, tool, api_client


def make_captcha_error(
    state="challenge-state",
    url=None,
):
    response = Response()
    response.status_code = 403
    response.request = Request("GET", "https://api.hh.ru/me").prepare()
    return CaptchaRequired(
        response,
        {
            "errors": [
                {
                    "type": "captcha_required",
                    "value": "captcha_required",
                    "captcha_url": url
                    or "https://hh.ru/account/captcha?state=" + state,
                }
            ]
        },
    )


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


def test_get_vacancy_tests_parses_lux_initial_state():
    vacancy = make_vacancy("101")
    operation, tool, api_client = make_operation(
        [vacancy],
        {"101": "<p>Build APIs for our platform</p>"},
    )
    lux_config = {
        "redirectConfig": {
            "nested": {
                "vacancyTests": {
                    "101": {"uidPk": "u1", "guid": "g1", "tasks": []}
                }
            }
        }
    }
    tool.get_redirect_config = lambda url: lux_config

    tests_data = operation._get_vacancy_tests("https://hh.ru/apply")

    assert tests_data == {"101": {"uidPk": "u1", "guid": "g1", "tasks": []}}


def test_get_vacancy_tests_raises_when_tests_missing():
    vacancy = make_vacancy("101")
    operation, tool, api_client = make_operation(
        [vacancy],
        {"101": "<p>Build APIs for our platform</p>"},
    )
    tool.get_redirect_config = lambda url: {"redirectConfig": {}}

    with pytest.raises(ValueError, match="tests not found."):
        operation._get_vacancy_tests("https://hh.ru/apply")


def test_unparsed_test_falls_back_to_regular_apply(caplog):
    """Тест вакансии не спарсился - откликаемся как на обычную вакансию."""
    vacancy = make_vacancy("101")
    vacancy["has_test"] = True
    operation, tool, api_client = make_operation(
        [vacancy],
        {"101": "<p>Build APIs for our platform</p>"},
    )
    tool.get_redirect_config = lambda url: {"redirectConfig": {}}

    caplog.set_level("WARNING", logger="hh_applicant_tool.operations")

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert len(api_client.post_calls) == 1
    assert api_client.post_calls[0]["params"]["vacancy_id"] == "101"
    assert any(
        "пробую откликнуться как на обычную вакансию" in record.getMessage()
        for record in caplog.records
    )


def test_max_responses_limits_applied_count():
    vacancies = [make_vacancy(str(100 + i)) for i in range(3)]
    operation, tool, api_client = make_operation(
        vacancies,
        {str(100 + i): f"<p>Description {i}</p>" for i in range(3)},
    )
    operation.max_responses = 2

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert len(api_client.post_calls) == 2


def test_cancel_event_stops_apply_loop():
    import threading

    vacancies = [make_vacancy(str(100 + i)) for i in range(3)]
    operation, tool, api_client = make_operation(
        vacancies,
        {str(100 + i): f"<p>Description {i}</p>" for i in range(3)},
    )
    cancel_event = threading.Event()
    cancel_event.set()
    operation._cancel_event = cancel_event

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert api_client.post_calls == []


def test_captcha_on_vacancy_description_is_not_swallowed():
    captcha_error = make_captcha_error()
    vacancy = make_vacancy("101")
    operation, _, api_client = make_operation(
        [vacancy],
        {},
        get_error=captcha_error,
    )

    with pytest.raises(CaptchaRequired):
        operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert api_client.post_calls == []


def test_captcha_on_apply_stops_remaining_vacancies():
    captcha_error = make_captcha_error()
    vacancies = [make_vacancy("101"), make_vacancy("202")]
    operation, _, api_client = make_operation(
        vacancies,
        {"101": "Description 101", "202": "Description 202"},
        post_error=captcha_error,
    )

    with pytest.raises(CaptchaRequired):
        operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert len(api_client.post_calls) == 1


def test_get_captcha_ai_uses_shared_env_and_disables_reasoning(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://vision.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "vision/test-model")
    monkeypatch.setenv("OPENAI_REASONING", "xhigh")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    operation = Operation()
    operation._captcha_ai = None
    operation.tool = SimpleNamespace(
        config={},
        openai_session=SimpleNamespace(proxies={}),
    )

    class FakeVisionClient:
        def __init__(self, config):
            self.config = config

    monkeypatch.setattr(
        "hh_applicant_tool.operations.apply_vacancies.OpenRouterChatClient",
        lambda config: FakeVisionClient(config),
    )

    client = operation._get_captcha_ai()

    assert client is operation._captcha_ai
    assert client.config.api_key == "test-key"
    assert client.config.base_url == "https://vision.example/v1"
    assert client.config.model == "vision/test-model"
    assert client.config.temperature == 0.0
    assert client.config.max_completion_tokens == 20
    assert client.config.reasoning_enabled is False
    assert client.config.reasoning_effort is None


def test_captcha_url_is_redacted_and_external_host_is_rejected():
    assert (
        Operation._safe_captcha_url(
            "https://hh.ru/account/captcha?state=secret"
        )
        == "https://hh.ru/account/captcha"
    )
    operation = Operation()
    operation.tool = SimpleNamespace(
        session=SimpleNamespace(cookies=[]),
        config={},
    )

    with pytest.raises(CaptchaSolveError, match="недопустимый адрес"):
        operation._handle_captcha_required(
            make_captcha_error(
                url="https://attacker.example/account/captcha?state=secret"
            )
        )


def test_captcha_url_rejects_nonstandard_port_and_userinfo():
    operation = Operation()

    for captcha_url in (
        "https://hh.ru:8443/account/captcha?state=secret",
        "https://user@hh.ru/account/captcha?state=secret",
    ):
        with pytest.raises(CaptchaSolveError, match="недопустимый адрес"):
            operation._handle_captcha_required(
                make_captcha_error(url=captcha_url)
            )


def test_playwright_captcha_flow_submits_and_syncs_only_hh_cookies(
    monkeypatch,
):
    calls = {}

    class FakeImage:
        async def screenshot(self):
            return b"captcha-image"

    class FakePage:
        async def goto(self, url, **kwargs):
            calls["url"] = url
            calls["goto_kwargs"] = kwargs
            return SimpleNamespace(status=200)

        async def wait_for_selector(self, selector, **kwargs):
            calls["image_selector"] = selector
            return FakeImage()

        async def fill(self, selector, text):
            calls["fill"] = (selector, text)

        async def press(self, selector, key):
            calls["press"] = (selector, key)

        async def wait_for_load_state(self, state, **kwargs):
            calls["load_state"] = state

        async def evaluate(self, script):
            calls["feedback_script"] = script
            return {
                "inputMaxLength": 10,
                "inputInvalid": False,
                "hasValidationAlert": False,
                "hasCaptchaErrorMarker": False,
                "rejectionMessage": False,
            }

    class FakeContext:
        async def add_cookies(self, cookies):
            calls["added_cookies"] = cookies

        async def new_page(self):
            return FakePage()

        async def cookies(self):
            return [
                {
                    "name": "captcha_session",
                    "value": "accepted",
                    "domain": ".hh.ru",
                    "path": "/",
                    "httpOnly": True,
                },
                {
                    "name": "foreign",
                    "value": "ignored",
                    "domain": "attacker.example",
                    "path": "/",
                },
            ]

    class FakeBrowser:
        async def new_context(self, **kwargs):
            calls["context_options"] = kwargs
            return FakeContext()

        async def close(self):
            calls["closed"] = True

    class FakeChromium:
        async def launch(self, **kwargs):
            calls["launch"] = kwargs
            return FakeBrowser()

    class FakePlaywright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *args):
            return None

    playwright_module = types.ModuleType("playwright")
    playwright_module.__path__ = []
    async_api_module = types.ModuleType("playwright.async_api")
    async_api_module.async_playwright = FakePlaywright
    async_api_module.TimeoutError = type("TimeoutError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_module)

    class FakeVisionClient:
        def solve_captcha(self, image_bytes):
            calls["ocr_image"] = image_bytes
            return "A1B2"

    operation = Operation()
    operation._captcha_ai = FakeVisionClient()
    operation.tool = SimpleNamespace(
        session=SimpleNamespace(cookies=RequestsCookieJar()),
        api_client=SimpleNamespace(user_agent="ru.hh.android/test"),
    )

    solved = asyncio.run(
        operation._solve_hh_captcha_async(
            "https://hh.ru/account/captcha?state=secret"
        )
    )

    assert solved is True
    assert calls["launch"] == {"headless": True}
    assert calls["context_options"] == {
        "user_agent": "ru.hh.android/test",
    }
    assert calls["image_selector"] == Operation.SEL_CAPTCHA_IMAGE
    assert calls["ocr_image"] == b"captcha-image"
    assert calls["fill"] == (Operation.SEL_CAPTCHA_INPUT, "A1B2")
    assert calls["press"] == (Operation.SEL_CAPTCHA_INPUT, "Enter")
    assert "aria-invalid" in calls["feedback_script"]
    assert "inputMaxLength" in calls["feedback_script"]
    assert "rejectionMessage" in calls["feedback_script"]
    assert calls["closed"] is True
    synced = list(operation.tool.session.cookies)
    assert [(cookie.name, cookie.domain) for cookie in synced] == [
        ("captcha_session", ".hh.ru")
    ]


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


def _make_pause_recorders(monkeypatch):
    import random as random_module
    import time as time_module

    uniform_calls = []
    sleep_calls = []

    def fake_uniform(a, b):
        uniform_calls.append((a, b))
        return 7.5

    monkeypatch.setattr(random_module, "uniform", fake_uniform)
    monkeypatch.setattr(time_module, "sleep", sleep_calls.append)
    return uniform_calls, sleep_calls


def test_apply_pauses_between_successes(monkeypatch):
    """Каждый успех -> пауза; uniform вызван с (min, max), sleep - с его значением."""
    vacancies = [make_vacancy(str(100 + i)) for i in range(3)]
    operation, tool, api_client = make_operation(
        vacancies,
        {str(100 + i): f"<p>Description {i}</p>" for i in range(3)},
    )
    operation.apply_delay_min = 30.0
    operation.apply_delay_max = 120.0
    uniform_calls, sleep_calls = _make_pause_recorders(monkeypatch)

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert len(api_client.post_calls) == 3
    assert (30.0, 120.0) in uniform_calls
    assert sleep_calls.count(7.5) == 3


def test_apply_does_not_sleep_in_dry_run(monkeypatch):
    """Dry-run ничего не отправляет и не спит (быстрая симуляция)."""
    vacancies = [make_vacancy(str(100 + i)) for i in range(2)]
    operation, tool, api_client = make_operation(
        vacancies,
        {str(100 + i): f"<p>Description {i}</p>" for i in range(2)},
        dry_run=True,
    )
    operation.apply_delay_min = 30.0
    operation.apply_delay_max = 120.0
    _, sleep_calls = _make_pause_recorders(monkeypatch)

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert api_client.post_calls == []
    assert sleep_calls == []


def test_apply_skips_pause_before_limit_stop(monkeypatch):
    """Паузы нет после последнего отклика перед выходом по лимиту."""
    vacancies = [make_vacancy(str(100 + i)) for i in range(3)]
    operation, tool, api_client = make_operation(
        vacancies,
        {str(100 + i): f"<p>Description {i}</p>" for i in range(3)},
    )
    operation.apply_delay_min = 30.0
    operation.apply_delay_max = 120.0
    operation.max_responses = 2
    _, sleep_calls = _make_pause_recorders(monkeypatch)

    operation._apply_resume(RESUME, USER, seen_employers={"501"})

    assert len(api_client.post_calls) == 2
    assert sleep_calls.count(7.5) == 1


def test_validate_apply_delays_rejects_bad_bounds():
    with pytest.raises(ValueError, match=">= 0"):
        validate_apply_delays(-1.0, 120.0)
    with pytest.raises(ValueError, match=">= 0"):
        validate_apply_delays(30.0, -5.0)
    with pytest.raises(ValueError, match="не может быть больше"):
        validate_apply_delays(120.0, 30.0)
    validate_apply_delays(30.0, 120.0)
    validate_apply_delays(0.0, 0.0)
