from __future__ import annotations

import sqlite3

from .repositories.agent_decisions import AgentDecisionRepository
from .repositories.agent_outbox import AgentOutboxRepository
from .repositories.agent_runs import AgentRunRepository
from .repositories.agent_webhooks import AgentWebhookRepository
from .repositories.application_attempts import ApplicationAttemptRepository
from .repositories.chat_messages import ChatMessageRepository
from .repositories.contacts import VacancyContactsRepository
from .repositories.employer_sites import EmployerSitesRepository
from .repositories.employers import EmployersRepository
from .repositories.mcp_runs import MCPRunRepository
from .repositories.negotiations import NegotiationRepository
from .repositories.resumes import ResumesRepository
from .repositories.settings import SettingsRepository
from .repositories.vacancies import VacanciesRepository
from .repositories.skipped_vacancies import SkippedVacanciesRepository
from .repositories.vacancy_analysis import VacancyAnalysisRepository
from .repositories.vacancy_response_dedup import VacancyResponseDedupRepository
from .utils import init_db


class StorageFacade:
    """Единая точка доступа к persistence-слою."""

    def __init__(self, conn: sqlite3.Connection):
        init_db(conn)
        self.agent_decisions = AgentDecisionRepository(conn)
        self.agent_outbox = AgentOutboxRepository(conn)
        self.agent_runs = AgentRunRepository(conn)
        self.agent_webhooks = AgentWebhookRepository(conn)
        self.application_attempts = ApplicationAttemptRepository(conn)
        self.chat_messages = ChatMessageRepository(conn)
        self.employer_sites = EmployerSitesRepository(conn)
        self.employers = EmployersRepository(conn)
        self.mcp_runs = MCPRunRepository(conn)
        self.negotiations = NegotiationRepository(conn)
        self.resumes = ResumesRepository(conn)
        self.settings = SettingsRepository(conn)
        self.vacancies = VacanciesRepository(conn)
        self.vacancy_analysis = VacancyAnalysisRepository(conn)
        self.vacancy_contacts = VacancyContactsRepository(conn)
        self.skipped_vacancies = SkippedVacanciesRepository(conn)
        self.vacancy_response_dedup = VacancyResponseDedupRepository(conn)
