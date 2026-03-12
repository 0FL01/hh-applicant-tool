import sqlite3
import sys
import types
from datetime import datetime
from types import SimpleNamespace

openai_module = types.ModuleType("openai")
openai_module.OpenAI = object
sys.modules.setdefault("openai", openai_module)

from hh_applicant_tool.storage import StorageFacade
from hh_llm_agent.config import AgentConfig, ClassifierConfig, OpenRouterConfig
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

    def fetch_messages(self, negotiation_id):
        assert negotiation_id == NEGOTIATION["id"]
        index = min(self.fetch_calls, len(self.responses) - 1)
        self.fetch_calls += 1
        return self.responses[index]

    def send_message(self, negotiation_id, message, delay=None):
        self.sent_messages.append((negotiation_id, message, delay))


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


def make_tool():
    return SimpleNamespace(storage=StorageFacade(sqlite3.connect(":memory:")))


def make_config(
    *,
    dry_run,
    incoming_collect_seconds=0,
    classifier_enabled=True,
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
    )


def make_service(
    monkeypatch,
    tool,
    gateway,
    *,
    dry_run,
    incoming_collect_seconds=0,
    classifier_enabled=True,
):
    monkeypatch.setattr(
        "hh_llm_agent.service.load_agent_config",
        lambda tool, args: make_config(
            dry_run=dry_run,
            incoming_collect_seconds=incoming_collect_seconds,
            classifier_enabled=classifier_enabled,
        ),
    )
    monkeypatch.setattr("hh_llm_agent.service.HHGateway", lambda tool: gateway)
    monkeypatch.setattr(
        "hh_llm_agent.service.OpenRouterChatClient",
        FakeLLMClient,
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


def test_classifier_skip_prevents_reply_generation(monkeypatch):
    FakeLLMClient.reset()
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
