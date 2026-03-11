import sqlite3
from types import SimpleNamespace

from hh_applicant_tool.storage import StorageFacade
from hh_llm_agent.config import AgentConfig, OpenRouterConfig
from hh_llm_agent.openrouter import LLMReply
from hh_llm_agent.service import ChatAgentService


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
}

RESUME = {
    "id": "resume-1",
    "title": "Platform Engineer",
    "status": {"id": "published"},
}

ME = {"first_name": "Ivan", "last_name": "Petrov"}


class FakeGateway:
    def __init__(self):
        self.sent_messages = []

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
        return [EMPLOYER_MESSAGE]

    def send_message(self, negotiation_id, message):
        self.sent_messages.append((negotiation_id, message))


class FakeLLMClient:
    instances = []

    def __init__(self, config):
        self.config = config
        self.calls = []
        self.__class__.instances.append(self)

    def complete_json(self, messages):
        self.calls.append(messages)
        return LLMReply(
            content='{"action":"reply","reply_text":"Здравствуйте!","reason":"need_reply"}',
            parsed={
                "action": "reply",
                "reply_text": "Здравствуйте!",
                "reason": "need_reply",
            },
        )


def make_tool():
    return SimpleNamespace(storage=StorageFacade(sqlite3.connect(":memory:")))


def make_config(*, dry_run):
    return AgentConfig(
        openrouter=OpenRouterConfig(api_key="token"),
        dry_run=dry_run,
    )


def make_service(monkeypatch, tool, gateway, *, dry_run):
    monkeypatch.setattr(
        "hh_llm_agent.service.load_agent_config",
        lambda tool, args: make_config(dry_run=dry_run),
    )
    monkeypatch.setattr("hh_llm_agent.service.HHGateway", lambda tool: gateway)
    monkeypatch.setattr(
        "hh_llm_agent.service.OpenRouterChatClient",
        FakeLLMClient,
    )
    return ChatAgentService(tool, SimpleNamespace())


def test_dry_run_does_not_persist_decisions_and_can_repeat(monkeypatch):
    FakeLLMClient.instances = []
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
    assert len(FakeLLMClient.instances) == 2
    assert len(FakeLLMClient.instances[0].calls) == 1
    assert len(FakeLLMClient.instances[1].calls) == 1


def test_existing_dry_run_decision_does_not_block_processing(monkeypatch):
    FakeLLMClient.instances = []
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
    assert len(FakeLLMClient.instances) == 1
    assert len(FakeLLMClient.instances[0].calls) == 1
    assert tool.storage.agent_decisions.count_total() == 1


def test_real_decision_still_blocks_reprocessing(monkeypatch):
    FakeLLMClient.instances = []
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
    assert len(FakeLLMClient.instances) == 1
    assert len(FakeLLMClient.instances[0].calls) == 0
