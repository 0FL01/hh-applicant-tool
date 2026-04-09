import sqlite3
import sys
import types
from datetime import datetime
from types import SimpleNamespace

from requests import Request, Response

openai_module = types.ModuleType("openai")
openai_module.OpenAI = object
sys.modules.setdefault("openai", openai_module)

from hh_applicant_tool.storage import StorageFacade
from hh_applicant_tool.api.errors import Forbidden
from hh_llm_agent.config import (
    AgentConfig,
    ClassifierConfig,
    OpenRouterConfig,
    WebhookConfig,
)
from hh_llm_agent.openrouter import LLMReply
from hh_llm_agent.service import (
    CLASSIFIER_RESPONSE_SCHEMA,
    REPLY_RESPONSE_SCHEMA,
    ChatAgentService,
)
from hh_llm_agent.timing import AgentTimingConfig, TimingPolicy


NEGOTIATION = {
    "id": 1,
    "chat_id": 11,
    "resume": {"id": "resume-1"},
    "state": {"id": "active"},
    "vacancy": {
        "id": 101,
        "name": "Platform Engineer",
        "alternate_url": "https://hh.ru/vacancy/101",
        "employer": {"id": 501, "name": "Acme"},
    },
    "updated_at": "2026-03-11T10:00:00+00:00",
}

EMPLOYER_MESSAGE = {
    "id": "msg-1",
    "text": "Добрый день! Расскажите подробнее о вашем опыте.",
    "author": {"participant_type": "employer"},
    "created_at": "2026-03-11T10:00:00+03:00",
}

RESUME = {
    "id": "resume-1",
    "title": "Platform Engineer",
    "status": {"id": "published"},
}

ME = {"first_name": "Ivan", "last_name": "Petrov"}

REPLY_MODEL = "reply-model"
CLASSIFIER_MODEL = "classifier-model"

DEFAULT_REPLY = LLMReply(
    content='{"action":"reply","reply_mode":"single","reply_text":"Здравствуйте!","reply_messages":[],"reason":"need_reply"}',
    parsed={
        "action": "reply",
        "reply_mode": "single",
        "reply_text": "Здравствуйте!",
        "reply_messages": [],
        "reason": "need_reply",
    },
)

DEFAULT_CLASSIFIER_REPLY = LLMReply(
    content='{"action":"reply","category":"human_actionable","reason":"direct_question","confidence":0.98}',
    parsed={
        "action": "reply",
        "category": "human_actionable",
        "reason": "direct_question",
        "confidence": 0.98,
    },
)


class FakeGateway:
    def __init__(self, responses=None):
        self.sent_messages = []
        self.responses = responses or [[EMPLOYER_MESSAGE]]
        self.fetch_calls = 0

    def get_user(self):
        return ME

    def get_resumes(self):
        return [RESUME]

    def get_blacklisted(self):
        return set()

    def get_negotiations(self):
        return [NEGOTIATION]

    def get_vacancy(self, vacancy_id):
        assert vacancy_id == NEGOTIATION["vacancy"]["id"]
        return {
            **NEGOTIATION["vacancy"],
            "contacts": {
                "name": "Виктория",
                "email": "v.vetkasova@wanted.ooo",
                "phones": [],
            },
        }

    def get_employer(self, employer_id):
        assert employer_id == NEGOTIATION["vacancy"]["employer"]["id"]
        return {
            **NEGOTIATION["vacancy"]["employer"],
            "site_url": "https://wanted.ooo",
            "alternate_url": "https://hh.ru/employer/501",
        }

    def fetch_messages(self, negotiation_id):
        assert negotiation_id == NEGOTIATION["id"]
        index = min(self.fetch_calls, len(self.responses) - 1)
        self.fetch_calls += 1
        return self.responses[index]

    def send_message(self, negotiation_id, message, delay=None):
        self.sent_messages.append((negotiation_id, message, delay))


class FailingGateway(FakeGateway):
    def send_message(self, negotiation_id, message, delay=None):
        response = Response()
        response.status_code = 403
        response._content = (
            b'{"description":"forbidden","errors":[{"type":"forbidden"}]}'
        )
        response.request = Request(
            "POST",
            f"https://api.hh.ru/negotiations/{negotiation_id}/messages",
        ).prepare()
        raise Forbidden(
            response,
            {"description": "forbidden", "errors": [{"type": "forbidden"}]},
        )


class FakeLLMClient:
    instances = []
    replies = {
        REPLY_MODEL: DEFAULT_REPLY,
        CLASSIFIER_MODEL: DEFAULT_CLASSIFIER_REPLY,
    }

    def __init__(self, config):
        self.config = config
        self.calls = []
        self.__class__.instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.replies = {
            REPLY_MODEL: DEFAULT_REPLY,
            CLASSIFIER_MODEL: DEFAULT_CLASSIFIER_REPLY,
        }

    @classmethod
    def by_model(cls, model):
        return [
            instance
            for instance in cls.instances
            if instance.config.model == model
        ]

    def complete_json(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.__class__.replies[self.config.model]


class FakeWebhookClient:
    instances = []
    should_fail = False

    def __init__(self, config, proxies=None):
        self.config = config
        self.proxies = proxies
        self.calls = []
        self.__class__.instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.should_fail = False

    def send(self, *, event_type, idempotency_key, payload):
        self.calls.append(
            {
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "payload": payload,
            }
        )
        if self.__class__.should_fail:
            raise RuntimeError("webhook delivery failed")


def make_tool():
    return SimpleNamespace(
        storage=StorageFacade(sqlite3.connect(":memory:")),
        session=SimpleNamespace(proxies={}),
    )


def make_config(
    *,
    dry_run,
    incoming_collect_seconds=0,
    classifier_enabled=True,
    webhook_enabled=False,
):
    return AgentConfig(
        openrouter=OpenRouterConfig(api_key="token", model=REPLY_MODEL),
        classifier=ClassifierConfig(
            enabled=classifier_enabled,
            openrouter=OpenRouterConfig(
                api_key="token",
                model=CLASSIFIER_MODEL,
                temperature=0.0,
                max_completion_tokens=128,
                reasoning_enabled=False,
            ),
        ),
        dry_run=dry_run,
        timing=AgentTimingConfig(
            sleep_min_minutes=20,
            sleep_max_minutes=30,
            quiet_hours_enabled=False,
            incoming_collect_seconds=incoming_collect_seconds,
            reply_delay_min_seconds=0,
            reply_delay_max_seconds=0,
            qa_series_delay_min_seconds=0,
            qa_series_delay_max_seconds=0,
            wake_jitter_seconds=0,
        ),
        webhook=(
            WebhookConfig(
                url="https://example.com/hook",
                timeout_seconds=3.0,
                max_attempts=3,
                retry_base_seconds=5.0,
            )
            if webhook_enabled
            else None
        ),
    )


def make_service(
    monkeypatch,
    tool,
    gateway,
    *,
    dry_run,
    incoming_collect_seconds=0,
    classifier_enabled=True,
    webhook_enabled=False,
):
    monkeypatch.setattr(
        "hh_llm_agent.service.load_agent_config",
        lambda tool, args: make_config(
            dry_run=dry_run,
            incoming_collect_seconds=incoming_collect_seconds,
            classifier_enabled=classifier_enabled,
            webhook_enabled=webhook_enabled,
        ),
    )
    monkeypatch.setattr("hh_llm_agent.service.HHGateway", lambda tool: gateway)
    monkeypatch.setattr(
        "hh_llm_agent.service.OpenRouterChatClient",
        FakeLLMClient,
    )
    monkeypatch.setattr(
        "hh_llm_agent.service.WebhookClient",
        FakeWebhookClient,
    )
    service = ChatAgentService(tool, SimpleNamespace())
    service.sleep_fn = lambda seconds: None
    return service


def test_dry_run_does_not_persist_decisions_and_can_repeat(monkeypatch):
    FakeLLMClient.reset()
    tool = make_tool()

    first_service = make_service(monkeypatch, tool, FakeGateway(), dry_run=True)
    first_stats = first_service.run()

    second_service = make_service(
        monkeypatch, tool, FakeGateway(), dry_run=True
    )
    second_stats = second_service.run()

    assert first_stats.replied == 1
    assert second_stats.replied == 1
    assert tool.storage.agent_decisions.count_total() == 0
    assert len(FakeLLMClient.by_model(REPLY_MODEL)) == 2
    assert len(FakeLLMClient.by_model(CLASSIFIER_MODEL)) == 2
    assert (
        sum(
            len(instance.calls)
            for instance in FakeLLMClient.by_model(REPLY_MODEL)
        )
        == 2
    )
    assert (
        sum(
            len(instance.calls)
            for instance in FakeLLMClient.by_model(CLASSIFIER_MODEL)
        )
        == 2
    )
    classifier_call = FakeLLMClient.by_model(CLASSIFIER_MODEL)[0].calls[0][1]
    reply_call = FakeLLMClient.by_model(REPLY_MODEL)[0].calls[0][1]
    assert classifier_call["schema"].name == CLASSIFIER_RESPONSE_SCHEMA.name
    assert reply_call["schema"].name == REPLY_RESPONSE_SCHEMA.name


def test_existing_dry_run_decision_does_not_block_processing(monkeypatch):
    FakeLLMClient.reset()
    tool = make_tool()
    tool.storage.agent_runs.save(
        {
            "id": "run-dry",
            "status": "completed",
            "dry_run": True,
        }
    )
    tool.storage.agent_decisions.save(
        {
            "id": "decision-dry",
            "run_id": "run-dry",
            "negotiation_id": NEGOTIATION["id"],
            "chat_id": NEGOTIATION["chat_id"],
            "vacancy_id": NEGOTIATION["vacancy"]["id"],
            "employer_id": NEGOTIATION["vacancy"]["employer"]["id"],
            "resume_id": NEGOTIATION["resume"]["id"],
            "last_message_id": EMPLOYER_MESSAGE["id"],
            "action": "reply",
            "reason": "need_reply",
            "reply_text": "old dry run",
            "model": "test-model",
            "raw_response": "{}",
            "reasoning_details": [],
        }
    )

    service = make_service(monkeypatch, tool, FakeGateway(), dry_run=True)
    stats = service.run()

    assert stats.replied == 1
    assert len(FakeLLMClient.by_model(REPLY_MODEL)[0].calls) == 1
    assert len(FakeLLMClient.by_model(CLASSIFIER_MODEL)[0].calls) == 1
    assert tool.storage.agent_decisions.count_total() == 1


def test_real_decision_still_blocks_reprocessing(monkeypatch):
    FakeLLMClient.reset()
    tool = make_tool()
    tool.storage.agent_runs.save(
        {
            "id": "run-real",
            "status": "completed",
            "dry_run": False,
        }
    )
    tool.storage.agent_decisions.save(
        {
            "id": "decision-real",
            "run_id": "run-real",
            "negotiation_id": NEGOTIATION["id"],
            "chat_id": NEGOTIATION["chat_id"],
            "vacancy_id": NEGOTIATION["vacancy"]["id"],
            "employer_id": NEGOTIATION["vacancy"]["employer"]["id"],
            "resume_id": NEGOTIATION["resume"]["id"],
            "last_message_id": EMPLOYER_MESSAGE["id"],
            "action": "reply",
            "reason": "need_reply",
            "reply_text": "real reply",
            "model": "test-model",
            "raw_response": "{}",
            "reasoning_details": [],
        }
    )

    service = make_service(monkeypatch, tool, FakeGateway(), dry_run=True)
    stats = service.run()

    assert stats.skipped == 1
    assert len(FakeLLMClient.by_model(REPLY_MODEL)[0].calls) == 0
    assert len(FakeLLMClient.by_model(CLASSIFIER_MODEL)[0].calls) == 0


def test_dry_run_skips_pending_outbox_flush(monkeypatch):
    FakeLLMClient.reset()
    tool = make_tool()
    tool.storage.agent_outbox.save(
        {
            "id": "outbox-1",
            "run_id": "old-run",
            "negotiation_id": NEGOTIATION["id"],
            "chat_id": NEGOTIATION["chat_id"],
            "source_last_message_id": EMPLOYER_MESSAGE["id"],
            "sequence_no": 1,
            "message_text": "old message",
            "status": "pending",
        }
    )
    gateway = FakeGateway(responses=[[]])

    service = make_service(monkeypatch, tool, gateway, dry_run=True)
    stats = service.run()

    assert stats.skipped == 1
    assert gateway.sent_messages == []
    outbox_item = next(tool.storage.agent_outbox.find())
    assert outbox_item.status == "pending"


def test_failed_pending_outbox_does_not_block_run(monkeypatch):
    FakeLLMClient.reset()
    tool = make_tool()
    tool.storage.agent_outbox.save(
        {
            "id": "outbox-1",
            "run_id": "old-run",
            "negotiation_id": NEGOTIATION["id"],
            "chat_id": NEGOTIATION["chat_id"],
            "source_last_message_id": EMPLOYER_MESSAGE["id"],
            "sequence_no": 1,
            "message_text": "old message",
            "status": "pending",
        }
    )
    gateway = FailingGateway(responses=[[]])

    service = make_service(monkeypatch, tool, gateway, dry_run=False)
    stats = service.run()

    assert stats.skipped == 1
    outbox_item = next(tool.storage.agent_outbox.find())
    assert outbox_item.status == "failed"
    assert outbox_item.last_error == "forbidden"


def test_classifier_skip_prevents_reply_generation(monkeypatch):
    FakeLLMClient.reset()
    FakeWebhookClient.reset()
    FakeLLMClient.replies[CLASSIFIER_MODEL] = LLMReply(
        content='{"action":"skip","category":"passive_update","reason":"auto_ack","confidence":0.99}',
        parsed={
            "action": "skip",
            "category": "passive_update",
            "reason": "auto_ack",
            "confidence": 0.99,
        },
    )
    tool = make_tool()

    service = make_service(monkeypatch, tool, FakeGateway(), dry_run=False)
    stats = service.run()

    assert stats.skipped == 1
    assert len(FakeLLMClient.by_model(CLASSIFIER_MODEL)[0].calls) == 1
    assert len(FakeLLMClient.by_model(REPLY_MODEL)[0].calls) == 0
    decisions = list(tool.storage.agent_decisions.find())
    assert len(decisions) == 1
    assert decisions[0].classifier_category == "passive_update"
    assert decisions[0].classifier_reason == "auto_ack"
    assert decisions[0].classifier_model == CLASSIFIER_MODEL


def test_recruiter_contact_offer_sends_webhook(monkeypatch):
    FakeLLMClient.reset()
    FakeWebhookClient.reset()
    recruiter_message = {
        **EMPLOYER_MESSAGE,
        "text": (
            "Андрей, здравствуйте!\n"
            "Меня зовут Виктория, я представляю команду IT-рекрутинга компании Wanted.\n"
            "Спасибо за ваш отклик. Хотела бы продолжить общение.\n"
            "С уважением,\n"
            "Веткасова Виктория\n"
            "telegram: t.me/ithr_victoria\n"
            "mail: v.vetkasova@wanted.ooo"
        ),
    }
    FakeLLMClient.replies[CLASSIFIER_MODEL] = LLMReply(
        content='{"action":"skip","category":"recruiter_contact_offer","reason":"direct_contacts_shared","confidence":0.97}',
        parsed={
            "action": "skip",
            "category": "recruiter_contact_offer",
            "reason": "direct_contacts_shared",
            "confidence": 0.97,
        },
    )
    tool = make_tool()
    gateway = FakeGateway(responses=[[recruiter_message]])

    service = make_service(
        monkeypatch,
        tool,
        gateway,
        dry_run=False,
        webhook_enabled=True,
    )
    stats = service.run()

    assert stats.skipped == 1
    assert len(FakeLLMClient.by_model(CLASSIFIER_MODEL)[0].calls) == 1
    assert len(FakeLLMClient.by_model(REPLY_MODEL)[0].calls) == 0
    assert len(FakeWebhookClient.instances) == 1
    assert len(FakeWebhookClient.instances[0].calls) == 1
    call = FakeWebhookClient.instances[0].calls[0]
    assert call["event_type"] == "recruiter_contact_offer"
    assert call["payload"]["vacancy"]["name"] == "Platform Engineer"
    assert call["payload"]["contacts"]["from_message"]["emails"] == [
        "v.vetkasova@wanted.ooo"
    ]
    assert call["payload"]["contacts"]["from_message"]["telegram_handles"] == [
        "ithr_victoria"
    ]
    decisions = list(tool.storage.agent_decisions.find())
    assert len(decisions) == 1
    assert decisions[0].classifier_category == "recruiter_contact_offer"
    webhook_items = list(tool.storage.agent_webhooks.find())
    assert len(webhook_items) == 1
    assert webhook_items[0].status == "sent"


def test_recruiter_contact_offer_without_real_contacts_is_downgraded(
    monkeypatch,
):
    """Classifier says recruiter_contact_offer but message has no contacts —
    guard should downgrade to irrelevant and skip without sending webhook."""
    FakeLLMClient.reset()
    FakeWebhookClient.reset()
    recruiter_message = {
        **EMPLOYER_MESSAGE,
        "text": (
            "Андрей, здравствуйте!\n"
            "Рассмотрим ваше резюме. Если навыки и опыт подойдут для позиции, "
            "мы свяжемся с вами.\n"
            "Конкина Александра"
        ),
    }
    FakeLLMClient.replies[CLASSIFIER_MODEL] = LLMReply(
        content='{"action":"skip","category":"recruiter_contact_offer","reason":"invites_contact","confidence":0.8}',
        parsed={
            "action": "skip",
            "category": "recruiter_contact_offer",
            "reason": "invites_contact",
            "confidence": 0.8,
        },
    )
    tool = make_tool()
    gateway = FakeGateway(responses=[[recruiter_message]])

    service = make_service(
        monkeypatch,
        tool,
        gateway,
        dry_run=False,
        webhook_enabled=True,
    )
    stats = service.run()

    assert stats.skipped == 1
    # No webhook should have been sent
    assert len(FakeWebhookClient.instances) == 1
    assert len(FakeWebhookClient.instances[0].calls) == 0
    # Decision should be saved with irrelevant category
    decisions = list(tool.storage.agent_decisions.find())
    assert len(decisions) == 1
    assert decisions[0].classifier_category == "irrelevant"
    assert decisions[0].reason == "no_contacts_found_in_message"
    # No webhook items in storage
    webhook_items = list(tool.storage.agent_webhooks.find())
    assert len(webhook_items) == 0


def test_qa_series_is_queued_and_sent_in_order(monkeypatch):
    FakeLLMClient.reset()
    FakeLLMClient.replies[REPLY_MODEL] = LLMReply(
        content='{"action":"reply","reply_mode":"qa_series","reply_text":"","reply_messages":["msg1","msg2"],"reason":"screening"}',
        parsed={
            "action": "reply",
            "reply_mode": "qa_series",
            "reply_text": "",
            "reply_messages": ["msg1", "msg2"],
            "reason": "screening",
        },
    )
    tool = make_tool()
    gateway = FakeGateway()

    service = make_service(monkeypatch, tool, gateway, dry_run=False)
    stats = service.run()

    assert stats.replied == 1
    assert [item[1] for item in gateway.sent_messages] == ["msg1", "msg2"]
    decisions = list(tool.storage.agent_decisions.find())
    assert len(decisions) == 1
    assert decisions[0].reply_text == "msg1\n\nmsg2"
    assert decisions[0].classifier_category == "human_actionable"
    outbox_items = list(tool.storage.agent_outbox.find())
    assert len(outbox_items) == 2
    assert all(item.status == "sent" for item in outbox_items)


def test_recent_messages_are_collected_before_llm_call(monkeypatch):
    FakeLLMClient.reset()
    FakeLLMClient.replies[REPLY_MODEL] = LLMReply(
        content='{"action":"reply","reply_mode":"single","reply_text":"combined","reply_messages":[],"reason":"need_reply"}',
        parsed={
            "action": "reply",
            "reply_mode": "single",
            "reply_text": "combined",
            "reply_messages": [],
            "reason": "need_reply",
        },
    )
    tool = make_tool()
    first_message = {
        **EMPLOYER_MESSAGE,
        "created_at": "2026-03-11T10:01:40+03:00",
    }
    second_message = {
        "id": "msg-2",
        "text": "Какие у вас ожидания по зарплате?",
        "author": {"participant_type": "employer"},
        "created_at": "2026-03-11T10:01:50+03:00",
    }
    gateway = FakeGateway(
        responses=[[first_message], [first_message, second_message]]
    )
    service = make_service(
        monkeypatch,
        tool,
        gateway,
        dry_run=True,
        incoming_collect_seconds=120,
    )
    slept = []
    service.sleep_fn = slept.append
    fixed_now = datetime.fromisoformat("2026-03-11T10:02:00+03:00")
    service.timing = TimingPolicy(
        service.config.timing,
        now_fn=lambda tz: fixed_now.astimezone(tz),
        uniform_fn=lambda a, b: a,
    )

    stats = service.run()

    assert stats.replied == 1
    assert gateway.fetch_calls == 2
    assert slept == [100.0]
    final_prompt = FakeLLMClient.by_model(REPLY_MODEL)[0].calls[0][0][-1][
        "content"
    ]
    assert "Добрый день!" in final_prompt
    assert "Какие у вас ожидания по зарплате?" in final_prompt


def test_run_logs_reply_summary(monkeypatch, caplog):
    FakeLLMClient.reset()
    tool = make_tool()
    service = make_service(monkeypatch, tool, FakeGateway(), dry_run=True)

    caplog.set_level("INFO", logger="hh_llm_agent")

    stats = service.run()

    assert stats.replied == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("Chat agent run started:" in message for message in messages)
    assert any("Negotiation 1 start:" in message for message in messages)
    assert any("Negotiation 1 classifier:" in message for message in messages)
    assert any("Negotiation 1 LLM decision:" in message for message in messages)
    assert any(
        "Negotiation 1 dry-run reply:" in message for message in messages
    )
    assert any("Chat agent run completed:" in message for message in messages)


def test_run_logs_skip_summary(monkeypatch, caplog):
    FakeLLMClient.reset()
    tool = make_tool()
    gateway = FakeGateway(responses=[[]])
    service = make_service(monkeypatch, tool, gateway, dry_run=True)

    caplog.set_level("INFO", logger="hh_llm_agent")

    stats = service.run()

    assert stats.skipped == 1
    assert any(
        "Negotiation 1 skipped: vacancy='Platform Engineer' employer='Acme' reason=no_messages"
        in record.getMessage()
        for record in caplog.records
    )


def _make_service_for_unit():
    """Create a minimal ChatAgentService instance for unit-testing internal methods."""
    tool = make_tool()
    config = make_config(dry_run=True)
    service = object.__new__(ChatAgentService)
    service.tool = tool
    service.config = config
    service.timing = TimingPolicy(config.timing)
    service.sleep_fn = lambda seconds: None
    return service


def _make_messages(qa_pairs: list[tuple[str, str]]):
    """Build alternating employer/applicant messages from (question, answer) pairs."""
    messages = []
    for idx, (question, answer) in enumerate(qa_pairs):
        messages.append(
            {
                "id": f"msg-e-{idx}",
                "text": question,
                "author": {"participant_type": "employer"},
                "created_at": f"2026-03-11T1{idx}:00:00+03:00",
            }
        )
        if answer:
            messages.append(
                {
                    "id": f"msg-a-{idx}",
                    "text": answer,
                    "author": {"participant_type": "applicant"},
                    "created_at": f"2026-03-11T1{idx}:01:00+03:00",
                }
            )
    return messages


def test_is_bot_loop_detects_repeated_question():
    service = _make_service_for_unit()
    messages = _make_messages(
        [
            (
                "Какое количество людей было у вас в подчинении?",
                "5-7 инженеров.",
            ),
            ("Используете ли GitOps?", "Да, ArgoCD."),
            (
                "Какое количество людей было у вас в подчинении?",
                "5-7 инженеров.",
            ),
            (
                "Какое количество людей было у вас в подчинении?",
                "5-7 инженеров.",
            ),
        ]
    )
    employer_tail = [
        {
            "id": "msg-tail",
            "text": "Какое количество людей было у вас в подчинении?",
            "author": {"participant_type": "employer"},
        }
    ]
    assert service._is_bot_loop(messages, employer_tail) is True


def test_is_bot_loop_not_triggered_by_different_questions():
    service = _make_service_for_unit()
    messages = _make_messages(
        [
            ("Какой опыт с Kubernetes?", "3 года."),
            ("Используете ли GitOps?", "Да, ArgoCD."),
        ]
    )
    employer_tail = [
        {
            "id": "msg-tail",
            "text": "Какая версия Kubernetes используется?",
            "author": {"participant_type": "employer"},
        }
    ]
    assert service._is_bot_loop(messages, employer_tail) is False


def test_is_bot_loop_not_triggered_by_multi_question_tail():
    service = _make_service_for_unit()
    messages = _make_messages(
        [
            ("Расскажите о себе.", "Я DevOps инженер."),
        ]
    )
    employer_tail = [
        {
            "id": "msg-tail-1",
            "text": "Какой опыт с Kubernetes?",
            "author": {"participant_type": "employer"},
        },
        {
            "id": "msg-tail-2",
            "text": "Зарплатные ожидания?",
            "author": {"participant_type": "employer"},
        },
    ]
    assert service._is_bot_loop(messages, employer_tail) is False


def test_is_bot_loop_not_triggered_when_no_prior_answer():
    service = _make_service_for_unit()
    messages = [
        {
            "id": "msg-e-0",
            "text": "Какое количество людей было у вас в подчинении?",
            "author": {"participant_type": "employer"},
        }
    ]
    employer_tail = [
        {
            "id": "msg-tail",
            "text": "Какое количество людей было у вас в подчинении?",
            "author": {"participant_type": "employer"},
        }
    ]
    assert service._is_bot_loop(messages, employer_tail) is False


def test_process_negotiation_skips_bot_loop_without_llm_calls(monkeypatch):
    FakeLLMClient.reset()
    tool = make_tool()
    bot_messages = _make_messages(
        [
            (
                "Какое количество людей было у вас в подчинении?",
                "5-7 инженеров.",
            ),
            (
                "Какое количество людей было у вас в подчинении?",
                "5-7 инженеров.",
            ),
            (
                "Какое количество людей было у вас в подчинении?",
                "5-7 инженеров.",
            ),
        ]
    )
    bot_messages.append(
        {
            "id": "msg-tail-final",
            "text": "Какое количество людей было у вас в подчинении?",
            "author": {"participant_type": "employer"},
            "created_at": "2026-03-11T17:37:00+03:00",
        }
    )
    gateway = FakeGateway(responses=[bot_messages])
    service = make_service(monkeypatch, tool, gateway, dry_run=True)

    stats = service.run()

    assert stats.skipped == 1
    assert stats.replied == 0
    classifier_instances = FakeLLMClient.by_model(CLASSIFIER_MODEL)
    reply_instances = FakeLLMClient.by_model(REPLY_MODEL)
    assert sum(len(i.calls) for i in classifier_instances) == 0
    assert sum(len(i.calls) for i in reply_instances) == 0
