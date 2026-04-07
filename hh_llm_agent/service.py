from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from hh_applicant_tool.api.errors import ApiError
from hh_applicant_tool.utils.date import parse_api_datetime, try_parse_datetime

from .contact_extract import extract_contact_details
from .config import AgentConfig, load_agent_config
from .gateway import HHGateway
from .openrouter import (
    LLMReply,
    OpenRouterChatClient,
    OpenRouterError,
    StructuredOutputSchema,
)
from .timing import TimingPolicy
from .webhook import WebhookClient, WebhookError

logger = logging.getLogger(__package__)

CLASSIFIER_CATEGORY_VALUES = (
    "human_actionable",
    "bot_actionable",
    "passive_update",
    "marketing_broadcast",
    "recruiter_contact_offer",
    "system_event",
    "irrelevant",
)
CLASSIFIER_CATEGORIES = set(CLASSIFIER_CATEGORY_VALUES)

CLASSIFIER_RESPONSE_SCHEMA = StructuredOutputSchema(
    name="chat_classifier_decision",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["reply", "skip"],
                "description": "Whether the candidate should reply now.",
            },
            "category": {
                "type": "string",
                "enum": list(CLASSIFIER_CATEGORY_VALUES),
                "description": "Classifier category for the employer tail.",
            },
            "reason": {
                "type": "string",
                "description": "Short explanation for the decision.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confidence score from 0 to 1.",
            },
        },
        "required": ["action", "category", "reason", "confidence"],
        "additionalProperties": False,
    },
)

REPLY_RESPONSE_SCHEMA = StructuredOutputSchema(
    name="chat_reply_decision",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["reply", "skip"],
                "description": "Whether to reply to the employer now.",
            },
            "reply_mode": {
                "type": "string",
                "enum": ["single", "qa_series"],
                "description": "Single message or short Q/A series.",
            },
            "reply_text": {
                "type": "string",
                "description": "Main reply text for single-mode replies.",
            },
            "reply_messages": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
                "description": "Ordered short messages for qa_series mode.",
            },
            "reason": {
                "type": "string",
                "description": "Short explanation for the decision.",
            },
        },
        "required": [
            "action",
            "reply_mode",
            "reply_text",
            "reply_messages",
            "reason",
        ],
        "additionalProperties": False,
    },
)


@dataclass
class RunStats:
    total: int = 0
    replied: int = 0
    skipped: int = 0
    errors: int = 0


class ChatAgentService:
    def __init__(self, tool, args):
        self.tool = tool
        self.args = args
        self.config: AgentConfig = load_agent_config(tool, args)
        self.gateway = HHGateway(tool)
        self.llm = OpenRouterChatClient(self.config.openrouter)
        self.classifier_llm = (
            OpenRouterChatClient(self.config.classifier.openrouter)
            if self.config.classifier and self.config.classifier.enabled
            else None
        )
        self.webhook_client = (
            WebhookClient(
                self.config.webhook,
                proxies=getattr(self.tool.session, "proxies", None),
            )
            if self.config.webhook and self.config.webhook.enabled
            else None
        )
        self.stats = RunStats()
        self.run_id = uuid4().hex
        self.timing = TimingPolicy(self.config.timing)
        self.sleep_fn = time.sleep

    def _negotiation_meta(self, negotiation: dict) -> tuple[str, str, str, str]:
        vacancy = negotiation.get("vacancy") or {}
        employer = vacancy.get("employer") or {}
        return (
            str(negotiation.get("id")),
            str(vacancy.get("name") or "-"),
            str(employer.get("name") or "-"),
            str((negotiation.get("resume") or {}).get("id") or "-"),
        )

    def _log_negotiation_start(self, negotiation: dict) -> None:
        negotiation_id, vacancy_name, employer_name, resume_id = (
            self._negotiation_meta(negotiation)
        )
        logger.info(
            "Negotiation %s start: vacancy=%r employer=%r state=%s resume=%s",
            negotiation_id,
            vacancy_name,
            employer_name,
            negotiation["state"]["id"],
            resume_id,
        )

    def _log_negotiation_outcome(
        self,
        negotiation: dict,
        outcome: str,
        **details: object,
    ) -> None:
        negotiation_id, vacancy_name, employer_name, _ = self._negotiation_meta(
            negotiation
        )
        detail_parts = [f"{key}={value}" for key, value in details.items()]
        suffix = f" {' '.join(detail_parts)}" if detail_parts else ""
        logger.info(
            "Negotiation %s %s: vacancy=%r employer=%r%s",
            negotiation_id,
            outcome,
            vacancy_name,
            employer_name,
            suffix,
        )

    def run(self) -> RunStats:
        self._save_run(status="running")
        try:
            logger.info(
                "Chat agent run started: run_id=%s dry_run=%s limit=%s resume_id=%s only_invitations=%s skip_blacklisted=%s force=%s classifier=%s webhook=%s",
                self.run_id,
                self.config.dry_run,
                self.config.limit,
                self.config.resume_id,
                self.config.only_invitations,
                self.config.skip_blacklisted,
                self.config.force,
                self.config.classifier.openrouter.model
                if self.classifier_llm
                else "disabled",
                self.config.webhook.url
                if self.webhook_client is not None
                else "disabled",
            )
            if self.config.dry_run:
                pending = self.tool.storage.agent_outbox.list_pending()
                if pending:
                    logger.info(
                        "Dry-run mode skips pending outbox flush: pending=%s",
                        len(pending),
                    )
                pending_webhooks = (
                    self.tool.storage.agent_webhooks.list_pending()
                )
                if pending_webhooks:
                    logger.info(
                        "Dry-run mode skips pending webhook flush: pending=%s",
                        len(pending_webhooks),
                    )
            else:
                self._flush_pending_webhooks()
                self._flush_pending_outbox()
            me = self.gateway.get_user()
            resumes = self._get_resume_map()
            blacklisted = (
                self.gateway.get_blacklisted()
                if self.config.skip_blacklisted
                else set()
            )
            logger.info(
                "Chat agent context loaded: candidate=%r resumes=%s blacklisted=%s",
                f"{(me.get('first_name') or '').strip()} {(me.get('last_name') or '').strip()}".strip()
                or "-",
                len(resumes),
                len(blacklisted),
            )

            for negotiation in self.gateway.get_negotiations():
                if self.config.limit and self.stats.total >= self.config.limit:
                    break
                self.stats.total += 1
                self._log_negotiation_start(negotiation)
                try:
                    self._process_negotiation(
                        negotiation=negotiation,
                        me=me,
                        resumes=resumes,
                        blacklisted=blacklisted,
                    )
                except (
                    ApiError,
                    OpenRouterError,
                    WebhookError,
                    ValueError,
                ) as ex:
                    self.stats.errors += 1
                    logger.warning(
                        "Chat agent failed for negotiation %s: %s",
                        negotiation.get("id"),
                        ex,
                    )
                except Exception as ex:
                    self.stats.errors += 1
                    logger.exception(
                        "Unexpected chat agent error for negotiation %s: %s",
                        negotiation.get("id"),
                        ex,
                    )

            self._save_run(status="completed")
            logger.info(
                "Chat agent run completed: run_id=%s total=%s replied=%s skipped=%s errors=%s",
                self.run_id,
                self.stats.total,
                self.stats.replied,
                self.stats.skipped,
                self.stats.errors,
            )
            return self.stats
        except Exception:
            self._save_run(status="failed")
            logger.info(
                "Chat agent run failed: run_id=%s total=%s replied=%s skipped=%s errors=%s",
                self.run_id,
                self.stats.total,
                self.stats.replied,
                self.stats.skipped,
                self.stats.errors,
            )
            raise

    def _get_resume_map(self) -> dict[str, dict]:
        resumes = [
            resume
            for resume in self.gateway.get_resumes()
            if resume["status"]["id"] == "published"
        ]
        if self.config.resume_id:
            resumes = [r for r in resumes if r["id"] == self.config.resume_id]
        return {resume["id"]: resume for resume in resumes}

    def _process_negotiation(
        self,
        negotiation: dict,
        me: dict,
        resumes: dict[str, dict],
        blacklisted: set[str],
    ) -> None:
        self.tool.storage.negotiations.save(negotiation)

        resume = resumes.get(negotiation["resume"]["id"])
        if not resume:
            self._skip(negotiation, reason="resume_not_selected")
            return

        updated_at = parse_api_datetime(negotiation["updated_at"])
        if (
            self.config.period_days is not None
            and (datetime.now(updated_at.tzinfo) - updated_at).days
            > self.config.period_days
        ):
            self._skip(negotiation, reason="outside_period")
            return

        state_id = negotiation["state"]["id"]
        if state_id == "discard":
            self._skip(negotiation, reason="discarded")
            return

        if self.config.only_invitations and not state_id.startswith("inv"):
            self._skip(negotiation, reason="not_invitation")
            return

        employer = (negotiation.get("vacancy") or {}).get("employer") or {}
        if employer.get("id") in blacklisted:
            self._skip(negotiation, reason="blacklisted")
            return

        messages = [
            message
            for message in self.gateway.fetch_messages(negotiation["id"])
            if message.get("text")
        ]
        if not messages:
            self._skip(negotiation, reason="no_messages")
            return

        self._save_messages(negotiation, messages)

        employer_tail = self._extract_employer_tail(messages)
        if not employer_tail:
            self._skip(
                negotiation,
                reason="last_message_not_from_employer",
                last_message=messages[-1],
            )
            return

        messages, employer_tail = self._collect_recent_employer_messages(
            negotiation,
            messages,
            employer_tail,
        )
        last_message = employer_tail[-1]

        self.tool.storage.agent_outbox.cancel_pending_for_negotiation(
            int(negotiation["id"]),
            str(last_message["id"]),
        )

        if not self.config.force and self._decision_exists(
            negotiation_id=negotiation["id"],
            last_message_id=last_message["id"],
        ):
            self._skip(
                negotiation,
                reason="already_processed",
                last_message=last_message,
                persist=False,
            )
            return

        classification = None
        classifier_reply = None
        if self.classifier_llm is not None:
            classifier_reply = self.classifier_llm.complete_json(
                self._build_classifier_messages(
                    negotiation=negotiation,
                    resume=resume,
                    me=me,
                    messages=messages,
                    unanswered_messages=employer_tail,
                ),
                schema=CLASSIFIER_RESPONSE_SCHEMA,
            )
            classification = self._normalize_classification(classifier_reply)
            logger.info(
                "Negotiation %s classifier: action=%s category=%s confidence=%.2f reason=%r",
                negotiation["id"],
                classification["action"],
                classification["category"],
                classification["confidence"],
                classification["reason"],
            )
            if classification["action"] != "reply":
                if classification["category"] == "recruiter_contact_offer":
                    self._handle_contact_webhook(
                        negotiation=negotiation,
                        resume=resume,
                        me=me,
                        unanswered_messages=employer_tail,
                        last_message=last_message,
                        classification=classification,
                        classifier_reply=classifier_reply,
                    )
                    return
                skip_reason = str(
                    classification["reason"] or classification["category"]
                )
                self._save_decision(
                    negotiation=negotiation,
                    last_message=last_message,
                    action="skip",
                    reason=skip_reason,
                    reply_text="",
                    raw_response="",
                    reasoning_details=[],
                    classifier_category=str(classification["category"]),
                    classifier_reason=skip_reason,
                    classifier_confidence=float(classification["confidence"]),
                    classifier_raw_response=classifier_reply.content,
                )
                self.stats.skipped += 1
                self._log_negotiation_outcome(
                    negotiation,
                    "skipped",
                    reason=skip_reason,
                    classifier_category=classification["category"],
                )
                print(
                    "⏭️ Пропущен чат "
                    f"{negotiation['id']}: {classification['category']}"
                )
                return

        reply = self.llm.complete_json(
            self._build_llm_messages(
                negotiation=negotiation,
                resume=resume,
                me=me,
                messages=messages,
                unanswered_messages=employer_tail,
            ),
            schema=REPLY_RESPONSE_SCHEMA,
        )
        decision = self._normalize_decision(reply)
        logger.info(
            "Negotiation %s LLM decision: action=%s reply_mode=%s messages=%s reason=%r",
            negotiation["id"],
            decision["action"],
            decision["reply_mode"],
            len(decision["reply_messages"]),
            decision["reason"],
        )

        if decision["action"] != "reply" or not decision["reply_messages"]:
            self._save_decision(
                negotiation=negotiation,
                last_message=last_message,
                action=decision["action"],
                reason=decision["reason"],
                reply_text=decision["reply_text"],
                raw_response=reply.content,
                reasoning_details=reply.reasoning_details,
                classifier_category=(
                    str(classification["category"]) if classification else None
                ),
                classifier_reason=(
                    str(classification["reason"] or "")
                    if classification
                    else None
                ),
                classifier_confidence=(
                    float(classification["confidence"])
                    if classification
                    else None
                ),
                classifier_raw_response=(
                    classifier_reply.content if classifier_reply else None
                ),
            )
            self.stats.skipped += 1
            self._log_negotiation_outcome(
                negotiation,
                "skipped",
                reason=decision["reason"] or "model_skip",
            )
            print(
                f"⏭️ Пропущен чат {negotiation['id']}: {decision['reason'] or 'model_skip'}"
            )
            return

        if self.config.dry_run:
            self.stats.replied += 1
            self._log_negotiation_outcome(
                negotiation,
                "dry-run reply",
                reply_mode=decision["reply_mode"],
                messages=len(decision["reply_messages"]),
            )
            print(
                f"🧪 dry-run чат {negotiation['id']}: {self._render_reply_text(decision)}"
            )
            return

        self._save_decision(
            negotiation=negotiation,
            last_message=last_message,
            action=decision["action"],
            reason=decision["reason"],
            reply_text=self._render_reply_text(decision),
            raw_response=reply.content,
            reasoning_details=reply.reasoning_details,
            classifier_category=(
                str(classification["category"]) if classification else None
            ),
            classifier_reason=(
                str(classification["reason"] or "") if classification else None
            ),
            classifier_confidence=(
                float(classification["confidence"]) if classification else None
            ),
            classifier_raw_response=(
                classifier_reply.content if classifier_reply else None
            ),
        )
        self._enqueue_reply_messages(
            negotiation=negotiation,
            last_message=last_message,
            decision=decision,
        )
        self.stats.replied += 1
        self._log_negotiation_outcome(
            negotiation,
            "reply queued",
            reply_mode=decision["reply_mode"],
            messages=len(decision["reply_messages"]),
        )
        self._dispatch_source_outbox(
            negotiation=int(negotiation["id"]),
            source_last_message_id=str(last_message["id"]),
            vacancy_url=(negotiation.get("vacancy") or {}).get("alternate_url"),
        )

    def _build_classifier_messages(
        self,
        negotiation: dict,
        resume: dict,
        me: dict,
        messages: list[dict],
        unanswered_messages: list[dict],
    ) -> list[dict[str, str]]:
        vacancy = negotiation.get("vacancy") or {}
        employer = vacancy.get("employer") or {}
        history_limit = (
            self.config.classifier.max_history_messages
            if self.config.classifier is not None
            else 8
        )
        history = messages[-history_limit:]
        history_block = "\n".join(
            f"[{message['author']['participant_type']}] {(message.get('text') or '').strip()}"
            for message in history
        )
        unanswered_block = "\n".join(
            f"{idx}. {(message.get('text') or '').strip()}"
            for idx, message in enumerate(unanswered_messages, 1)
        )

        classifier_messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": self.config.classifier.system_prompt,
            },
            {
                "role": "user",
                "content": (
                    f"Контекст:\n"
                    f"- Кандидат: {(me.get('first_name') or '').strip()} {(me.get('last_name') or '').strip()}\n"
                    f"- Резюме: {resume.get('title') or ''}\n"
                    f"- Вакансия: {vacancy.get('name') or ''}\n"
                    f"- Работодатель: {employer.get('name') or ''}\n"
                    f"- Статус отклика: {negotiation['state']['id']}\n"
                    f"- Screening mode: {'yes' if self._is_screening_sequence(unanswered_messages) else 'no'}.\n\n"
                    f"Последние сообщения:\n{history_block or '-'}\n\n"
                    f"Последний неотвеченный пакет работодателя:\n{unanswered_block}\n\n"
                    f"{self.config.classifier.instruction}"
                ).strip(),
            },
        ]

        return classifier_messages

    def _build_llm_messages(
        self,
        negotiation: dict,
        resume: dict,
        me: dict,
        messages: list[dict],
        unanswered_messages: list[dict],
    ) -> list[dict[str, str]]:
        vacancy = negotiation.get("vacancy") or {}
        employer = vacancy.get("employer") or {}
        history = messages[-self.config.max_history_messages :]
        unanswered_block = "\n".join(
            f"{idx}. {(message.get('text') or '').strip()}"
            for idx, message in enumerate(unanswered_messages, 1)
        )

        llm_messages: list[dict[str, str]] = [
            {"role": "system", "content": self.config.system_prompt},
            {
                "role": "user",
                "content": (
                    f"Контекст:\n"
                    f"- Кандидат: {(me.get('first_name') or '').strip()} {(me.get('last_name') or '').strip()}\n"
                    f"- Резюме: {resume.get('title') or ''}\n"
                    f"- Вакансия: {vacancy.get('name') or ''}\n"
                    f"- Работодатель: {employer.get('name') or ''}\n"
                    f"- Статус отклика: {negotiation['state']['id']}\n"
                    f"- Отвечать нужно от лица кандидата.\n"
                    f"- Screening mode: {'yes' if self._is_screening_sequence(unanswered_messages) else 'no'}."
                ).strip(),
            },
        ]

        for message in history:
            role = (
                "user"
                if message["author"]["participant_type"] == "employer"
                else "assistant"
            )
            llm_messages.append(
                {
                    "role": role,
                    "content": (message.get("text") or "").strip(),
                }
            )

        llm_messages.append(
            {
                "role": "user",
                "content": (
                    f"Неотвеченный пакет сообщений работодателя:\n{unanswered_block}\n\n"
                    f"{self.config.reply_instruction}\n"
                    "Правила:\n"
                    "- Если это screening или anti-bot вопросы, отвечай предметно, без пустых фраз.\n"
                    "- Если работодатель просит уточнить прошлый ответ, добавляй конкретику, а не повторяй прежнюю формулировку.\n"
                    "- Если вопрос требует данных от работодателя, корректно уточни их и не выдумывай факты о кандидате.\n"
                    "- Если во входящем пакете несколько отдельных вопросов, ответь на каждый по порядку.\n"
                    "- Для qa_series верни 2-3 коротких сообщения без markdown и эмодзи."
                ),
            }
        )
        return llm_messages

    def _build_idempotency_key(
        self,
        *,
        event_type: str,
        negotiation_id: str | int,
        last_message_id: str,
    ) -> str:
        return f"{event_type}:{negotiation_id}:{last_message_id}"

    def _storage_contact_records(
        self, vacancy_id: object
    ) -> list[dict[str, object]]:
        if vacancy_id is None:
            return []
        return [
            contact.to_dict()
            for contact in self.tool.storage.vacancy_contacts.find(
                vacancy_id=vacancy_id
            )
        ]

    def _storage_employer_sites(
        self, employer_id: object
    ) -> list[dict[str, object]]:
        if employer_id is None:
            return []
        return [
            site.to_dict()
            for site in self.tool.storage.employer_sites.find(
                employer_id=employer_id
            )
        ]

    def _load_webhook_context(self, negotiation: dict) -> tuple[dict, dict]:
        vacancy = dict(negotiation.get("vacancy") or {})
        employer = dict(vacancy.get("employer") or {})
        vacancy_id = vacancy.get("id")
        employer_id = employer.get("id")

        if vacancy_id:
            try:
                live_vacancy = self.gateway.get_vacancy(vacancy_id)
            except ApiError as ex:
                logger.warning(
                    "Failed to load vacancy %s for webhook context: %s",
                    vacancy_id,
                    ex,
                )
            else:
                vacancy.update(live_vacancy)
                try:
                    self.tool.storage.vacancies.save(live_vacancy)
                except Exception:
                    logger.debug(
                        "Failed to persist vacancy %s for webhook context",
                        vacancy_id,
                        exc_info=True,
                    )
                if live_vacancy.get("contacts"):
                    try:
                        self.tool.storage.vacancy_contacts.save(live_vacancy)
                    except Exception:
                        logger.debug(
                            "Failed to persist vacancy contacts for %s",
                            vacancy_id,
                            exc_info=True,
                        )

        if employer_id:
            try:
                live_employer = self.gateway.get_employer(employer_id)
            except ApiError as ex:
                logger.warning(
                    "Failed to load employer %s for webhook context: %s",
                    employer_id,
                    ex,
                )
            else:
                employer.update(live_employer)
                try:
                    self.tool.storage.employers.save(live_employer)
                except Exception:
                    logger.debug(
                        "Failed to persist employer %s for webhook context",
                        employer_id,
                        exc_info=True,
                    )

        vacancy["employer"] = employer
        return vacancy, employer

    def _build_contact_webhook_payload(
        self,
        *,
        negotiation: dict,
        resume: dict,
        me: dict,
        unanswered_messages: list[dict],
        classification: dict[str, object],
        last_message: dict,
    ) -> tuple[str, dict[str, object]]:
        vacancy, employer = self._load_webhook_context(negotiation)
        vacancy_id = vacancy.get("id")
        employer_id = employer.get("id")
        tail_messages = [
            {
                "id": message.get("id"),
                "created_at": message.get("created_at"),
                "text": (message.get("text") or "").strip(),
            }
            for message in unanswered_messages
        ]
        tail_text = "\n\n".join(
            message["text"] for message in tail_messages if message["text"]
        )
        extracted_contacts = extract_contact_details(tail_text)
        idempotency_key = self._build_idempotency_key(
            event_type=str(classification["category"]),
            negotiation_id=negotiation["id"],
            last_message_id=str(last_message.get("id") or "-"),
        )
        payload = {
            "event_type": str(classification["category"]),
            "idempotency_key": idempotency_key,
            "created_at": self.timing.now().isoformat(),
            "run_id": self.run_id,
            "classifier": {
                "category": classification["category"],
                "action": classification["action"],
                "reason": classification["reason"],
                "confidence": classification["confidence"],
            },
            "candidate": {
                "first_name": (me.get("first_name") or "").strip(),
                "last_name": (me.get("last_name") or "").strip(),
                "resume_id": resume.get("id"),
                "resume_title": resume.get("title"),
            },
            "negotiation": {
                "id": negotiation.get("id"),
                "chat_id": negotiation.get("chat_id"),
                "state": (negotiation.get("state") or {}).get("id"),
                "last_message_id": last_message.get("id"),
                "updated_at": negotiation.get("updated_at"),
            },
            "vacancy": {
                "id": vacancy_id,
                "name": vacancy.get("name"),
                "alternate_url": vacancy.get("alternate_url"),
                "area": {
                    "id": (vacancy.get("area") or {}).get("id")
                    or vacancy.get("area_id"),
                    "name": (vacancy.get("area") or {}).get("name")
                    or vacancy.get("area_name"),
                },
                "salary": vacancy.get("salary")
                or {
                    "from": vacancy.get("salary_from"),
                    "to": vacancy.get("salary_to"),
                    "currency": vacancy.get("currency"),
                    "gross": vacancy.get("gross"),
                },
            },
            "employer": {
                "id": employer_id,
                "name": employer.get("name"),
                "site_url": employer.get("site_url"),
                "alternate_url": employer.get("alternate_url"),
            },
            "contacts": {
                "from_message": extracted_contacts,
                "from_vacancy": {
                    "contacts": self._storage_contact_records(vacancy_id),
                    "raw_contacts": vacancy.get("contacts") or {},
                },
                "from_employer_site": self._storage_employer_sites(employer_id),
            },
            "employer_tail": {
                "messages": tail_messages,
                "text": tail_text,
            },
        }
        return idempotency_key, payload

    def _enqueue_webhook(
        self,
        *,
        negotiation: dict,
        last_message: dict,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        if self.config.dry_run or self.config.webhook is None:
            return
        self.tool.storage.agent_webhooks.save(
            {
                "id": uuid4().hex,
                "run_id": self.run_id,
                "negotiation_id": negotiation["id"],
                "chat_id": negotiation.get("chat_id"),
                "source_last_message_id": last_message.get("id"),
                "event_type": event_type,
                "target_url": self.config.webhook.url,
                "payload_json": payload,
                "send_after": self.timing.now().isoformat(),
                "status": "pending",
                "attempts_count": 0,
            }
        )

    def _deliver_webhook_item(self, item) -> None:
        if self.webhook_client is None:
            raise WebhookError("webhook client is disabled")
        payload = dict(item.payload_json or {})
        idempotency_key = str(
            payload.get("idempotency_key")
            or self._build_idempotency_key(
                event_type=item.event_type,
                negotiation_id=item.negotiation_id,
                last_message_id=item.source_last_message_id,
            )
        )
        self.webhook_client.send(
            event_type=item.event_type,
            idempotency_key=idempotency_key,
            payload=payload,
        )

    def _handle_webhook_delivery_error(self, item, error: str) -> None:
        attempts_count = int(item.attempts_count or 0) + 1
        if self.config.webhook is None:
            self.tool.storage.agent_webhooks.mark_failed(
                item.id,
                attempts_count=attempts_count,
                error=error,
            )
            return
        if attempts_count >= self.config.webhook.max_attempts:
            self.tool.storage.agent_webhooks.mark_failed(
                item.id,
                attempts_count=attempts_count,
                error=error,
            )
            return
        retry_delay = self.config.webhook.retry_base_seconds * (
            2 ** max(0, attempts_count - 1)
        )
        self.tool.storage.agent_webhooks.reschedule(
            item.id,
            attempts_count=attempts_count,
            error=error,
            send_after=self.timing.now() + timedelta(seconds=retry_delay),
        )

    def _dispatch_source_webhooks(
        self,
        negotiation: int,
        source_last_message_id: str,
    ) -> None:
        pending = self.tool.storage.agent_webhooks.list_pending_for_source(
            negotiation,
            source_last_message_id,
        )
        for item in pending:
            self._wait_until(item.send_after, item.id)
            try:
                self._deliver_webhook_item(item)
            except WebhookError as ex:
                self._handle_webhook_delivery_error(item, str(ex))
                raise
            self.tool.storage.agent_webhooks.mark_sent(
                item.id, self.timing.now()
            )

    def _flush_pending_webhooks(self) -> None:
        if self.webhook_client is None:
            return
        for item in self.tool.storage.agent_webhooks.list_pending():
            self._wait_until(item.send_after, item.id)
            try:
                self._deliver_webhook_item(item)
            except WebhookError as ex:
                self._handle_webhook_delivery_error(item, str(ex))
                logger.warning(
                    "Webhook flush failed for item %s negotiation=%s: %s",
                    item.id,
                    item.negotiation_id,
                    ex,
                )
                continue
            self.tool.storage.agent_webhooks.mark_sent(
                item.id, self.timing.now()
            )

    def _handle_contact_webhook(
        self,
        *,
        negotiation: dict,
        resume: dict,
        me: dict,
        unanswered_messages: list[dict],
        last_message: dict,
        classification: dict[str, object],
        classifier_reply: LLMReply,
    ) -> None:
        skip_reason = str(
            classification["reason"] or classification["category"]
        )
        idempotency_key, payload = self._build_contact_webhook_payload(
            negotiation=negotiation,
            resume=resume,
            me=me,
            unanswered_messages=unanswered_messages,
            classification=classification,
            last_message=last_message,
        )

        # Guard: verify that contacts were actually extracted from message
        # text. Classifier LLM may incorrectly label a message as
        # recruiter_contact_offer when no real contact info is present.
        from_contacts = (payload.get("contacts") or {}).get(
            "from_message"
        ) or {}
        has_real_contacts = bool(
            from_contacts.get("emails")
            or from_contacts.get("telegram_urls")
            or from_contacts.get("telegram_handles")
            or from_contacts.get("phones")
        )
        if not has_real_contacts:
            logger.info(
                "Negotiation %s: classifier said recruiter_contact_offer "
                "but no contacts found in message text — downgrading to "
                "irrelevant",
                negotiation["id"],
            )
            self._save_decision(
                negotiation=negotiation,
                last_message=last_message,
                action="skip",
                reason="no_contacts_found_in_message",
                reply_text="",
                raw_response="",
                reasoning_details=[],
                classifier_category="irrelevant",
                classifier_reason="classifier_false_positive_recruiter_contact_offer",
                classifier_confidence=float(classification["confidence"]),
                classifier_raw_response=classifier_reply.content,
            )
            self.stats.skipped += 1
            self._log_negotiation_outcome(
                negotiation,
                "skipped",
                reason="no_contacts_found_in_message",
                classifier_category="irrelevant",
            )
            print(
                f"⏭️ Пропущен чат {negotiation['id']}: "
                "no_contacts_found_in_message"
            )
            return

        self._save_decision(
            negotiation=negotiation,
            last_message=last_message,
            action="skip",
            reason=skip_reason,
            reply_text="",
            raw_response="",
            reasoning_details=[],
            classifier_category=str(classification["category"]),
            classifier_reason=skip_reason,
            classifier_confidence=float(classification["confidence"]),
            classifier_raw_response=classifier_reply.content,
        )
        self.stats.skipped += 1
        if self.config.dry_run:
            self._log_negotiation_outcome(
                negotiation,
                "webhook dry-run",
                event=str(classification["category"]),
                idempotency_key=idempotency_key,
            )
            print(
                f"🧪 dry-run webhook чат {negotiation['id']}: {payload['contacts']['from_message']}"
            )
            return
        if self.webhook_client is None or self.config.webhook is None:
            self._log_negotiation_outcome(
                negotiation,
                "skipped",
                reason=skip_reason,
                classifier_category=classification["category"],
                webhook="disabled",
            )
            return
        self._enqueue_webhook(
            negotiation=negotiation,
            last_message=last_message,
            event_type=str(classification["category"]),
            payload=payload,
        )
        self._log_negotiation_outcome(
            negotiation,
            "webhook queued",
            event=str(classification["category"]),
            idempotency_key=idempotency_key,
        )
        self._dispatch_source_webhooks(
            negotiation=int(negotiation["id"]),
            source_last_message_id=str(last_message["id"]),
        )

    def _normalize_classification(self, reply: LLMReply) -> dict[str, object]:
        payload = reply.parsed or {}
        action = str(payload.get("action") or "skip").strip().lower()
        if action not in {"reply", "skip"}:
            action = "skip"

        category = str(payload.get("category") or "irrelevant").strip().lower()
        if category not in CLASSIFIER_CATEGORIES:
            category = "human_actionable" if action == "reply" else "irrelevant"

        reason = str(payload.get("reason") or "").strip()

        try:
            confidence = float(payload.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(confidence, 1.0))

        if action == "reply" and category not in {
            "human_actionable",
            "bot_actionable",
        }:
            category = "human_actionable"

        if action == "skip" and category in {
            "human_actionable",
            "bot_actionable",
        }:
            category = "irrelevant"

        return {
            "action": action,
            "category": category,
            "reason": reason,
            "confidence": confidence,
        }

    def _normalize_decision(self, reply: LLMReply) -> dict[str, object]:
        payload = reply.parsed or {}
        action = str(payload.get("action") or "skip").strip().lower()
        if action not in {"reply", "skip"}:
            action = "skip"
        reply_mode = str(payload.get("reply_mode") or "single").strip().lower()
        reply_text = str(payload.get("reply_text") or "").strip()
        reply_messages = [
            str(item).strip()
            for item in (payload.get("reply_messages") or [])
            if str(item).strip()
        ]
        reason = str(payload.get("reason") or "").strip()

        if reply_mode not in {"single", "qa_series"}:
            reply_mode = "qa_series" if reply_messages else "single"

        if (
            action == "reply"
            and reply_mode == "qa_series"
            and not reply_messages
        ):
            if reply_text:
                reply_mode = "single"
            else:
                action = "skip"
                reason = reason or "empty_reply"

        if action == "reply" and reply_mode == "single" and not reply_text:
            if reply_messages:
                reply_text = " ".join(reply_messages)
            else:
                action = "skip"
                reason = reason or "empty_reply"

        if action == "reply" and reply_mode == "single":
            reply_messages = [reply_text]
        elif action == "reply" and len(reply_messages) == 1:
            reply_mode = "single"
            reply_text = reply_messages[0]

        if action == "reply" and not reply_messages:
            action = "skip"
            reason = reason or "empty_reply"

        return {
            "action": action,
            "reply_mode": reply_mode,
            "reply_text": reply_text,
            "reply_messages": reply_messages,
            "reason": reason,
        }

    def _decision_exists(
        self,
        negotiation_id: str | int,
        last_message_id: str,
    ) -> bool:
        decision = next(
            self.tool.storage.agent_decisions.find(
                negotiation_id=int(negotiation_id),
                last_message_id=str(last_message_id),
            ),
            None,
        )
        if decision is None:
            return False
        if decision.run_id:
            run = self.tool.storage.agent_runs.get(decision.run_id)
            if run and run.dry_run:
                return False
        return True

    def _save_messages(self, negotiation: dict, messages: list[dict]) -> None:
        items = []
        for message in messages:
            items.append(
                {
                    **message,
                    "negotiation_id": negotiation["id"],
                    "chat_id": negotiation.get("chat_id"),
                }
            )
        self.tool.storage.chat_messages.save_batch(items)

    def _save_run(self, status: str) -> None:
        self.tool.storage.agent_runs.save(
            {
                "id": self.run_id,
                "status": status,
                "model": self.config.openrouter.model,
                "dry_run": self.config.dry_run,
                "total_negotiations": self.stats.total,
                "replied_count": self.stats.replied,
                "skipped_count": self.stats.skipped,
                "error_count": self.stats.errors,
                "finished_at": datetime.now().isoformat()
                if status != "running"
                else None,
            }
        )

    def _extract_employer_tail(self, messages: list[dict]) -> list[dict]:
        tail = []
        for message in reversed(messages):
            if message["author"]["participant_type"] != "employer":
                break
            tail.append(message)
        return list(reversed(tail))

    def _message_timestamp(
        self,
        message: dict,
        negotiation: dict,
    ) -> datetime:
        parsed = try_parse_datetime(message.get("created_at"))
        if isinstance(parsed, datetime):
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=self.timing.tz)
            return parsed
        return parse_api_datetime(negotiation["updated_at"])

    def _collect_recent_employer_messages(
        self,
        negotiation: dict,
        messages: list[dict],
        employer_tail: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        if not employer_tail:
            return messages, employer_tail
        delay = self.timing.incoming_collect_delay(
            self._message_timestamp(employer_tail[-1], negotiation)
        )
        if delay <= 0:
            return messages, employer_tail
        self._sleep(delay, f"collect employer tail for {negotiation['id']}")
        refreshed = [
            message
            for message in self.gateway.fetch_messages(negotiation["id"])
            if message.get("text")
        ]
        if not refreshed:
            return messages, employer_tail
        self._save_messages(negotiation, refreshed)
        return refreshed, self._extract_employer_tail(refreshed)

    def _is_screening_sequence(self, unanswered_messages: list[dict]) -> bool:
        combined = "\n".join(
            (message.get("text") or "").lower()
            for message in unanswered_messages
        )
        markers = (
            "несколько вопросов",
            "пару минут",
            "это займет",
            "начнем",
            "подробнее",
            "какие именно",
            "уточните",
        )
        return len(unanswered_messages) > 1 or any(
            marker in combined for marker in markers
        )

    def _render_reply_text(self, decision: dict[str, object]) -> str:
        return "\n\n".join(
            str(item).strip()
            for item in decision.get("reply_messages", [])
            if str(item).strip()
        )

    def _enqueue_reply_messages(
        self,
        negotiation: dict,
        last_message: dict,
        decision: dict[str, object],
    ) -> None:
        delays = self.timing.message_schedule_delays(
            len(decision["reply_messages"])
        )
        now = self.timing.now()
        cumulative_delay = 0.0
        items = []
        for index, message_text in enumerate(decision["reply_messages"], 1):
            cumulative_delay += delays[index - 1]
            items.append(
                {
                    "id": uuid4().hex,
                    "run_id": self.run_id,
                    "negotiation_id": negotiation["id"],
                    "chat_id": negotiation.get("chat_id"),
                    "source_last_message_id": last_message["id"],
                    "sequence_no": index,
                    "message_text": str(message_text),
                    "send_after": (
                        now + timedelta(seconds=cumulative_delay)
                    ).isoformat(),
                    "status": "pending",
                }
            )
        self.tool.storage.agent_outbox.save_batch(items)

    def _dispatch_source_outbox(
        self,
        negotiation: int,
        source_last_message_id: str,
        vacancy_url: str | None,
    ) -> None:
        pending = self.tool.storage.agent_outbox.list_pending_for_source(
            negotiation,
            source_last_message_id,
        )
        for item in pending:
            if self.timing.in_quiet_hours():
                logger.info(
                    "Quiet hours reached before sending outbox item %s",
                    item.id,
                )
                return
            self._wait_until(item.send_after, item.id)
            if self.timing.in_quiet_hours():
                logger.info(
                    "Quiet hours reached while waiting for outbox item %s",
                    item.id,
                )
                return
            try:
                self.gateway.send_message(negotiation, item.message_text)
            except ApiError as ex:
                self.tool.storage.agent_outbox.mark_failed(item.id, str(ex))
                raise
            self.tool.storage.agent_outbox.mark_sent(item.id, self.timing.now())
            if vacancy_url:
                print(f"📨 Ответ отправлен для {vacancy_url}")
            else:
                print(f"📨 Ответ отправлен для чата {negotiation}")

    def _flush_pending_outbox(self) -> None:
        for item in self.tool.storage.agent_outbox.list_pending():
            if self.timing.in_quiet_hours():
                logger.info("Outbox flush paused by quiet hours")
                return
            self._wait_until(item.send_after, item.id)
            if self.timing.in_quiet_hours():
                logger.info("Outbox flush stopped by quiet hours")
                return
            try:
                self.gateway.send_message(
                    item.negotiation_id, item.message_text
                )
            except ApiError as ex:
                self.tool.storage.agent_outbox.mark_failed(item.id, str(ex))
                logger.warning(
                    "Outbox flush failed for item %s negotiation=%s: %s",
                    item.id,
                    item.negotiation_id,
                    ex,
                )
                continue
            self.tool.storage.agent_outbox.mark_sent(item.id, self.timing.now())

    def _wait_until(
        self,
        send_after: datetime | None,
        outbox_id: str,
    ) -> None:
        if send_after is None:
            return
        if not isinstance(send_after, datetime):
            parsed = try_parse_datetime(send_after)
            if isinstance(parsed, datetime):
                send_after = parsed
            else:
                return
        delay = (
            send_after.astimezone(self.timing.tz) - self.timing.now()
        ).total_seconds()
        if delay > 0:
            self._sleep(delay, f"wait for outbox item {outbox_id}")

    def _sleep(self, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        logger.info("Chat agent sleeps %.1f seconds: %s", seconds, reason)
        self.sleep_fn(seconds)

    def _save_decision(
        self,
        negotiation: dict,
        last_message: dict,
        action: str,
        reason: str,
        reply_text: str,
        raw_response: str,
        reasoning_details,
        classifier_category: str | None = None,
        classifier_reason: str | None = None,
        classifier_confidence: float | None = None,
        classifier_raw_response: str | None = None,
    ) -> None:
        if self.config.dry_run:
            return
        vacancy = negotiation.get("vacancy") or {}
        employer = vacancy.get("employer") or {}
        self.tool.storage.agent_decisions.save(
            {
                "id": uuid4().hex,
                "run_id": self.run_id,
                "negotiation_id": negotiation["id"],
                "chat_id": negotiation.get("chat_id"),
                "vacancy_id": vacancy.get("id"),
                "employer_id": employer.get("id"),
                "resume_id": negotiation.get("resume", {}).get("id"),
                "last_message_id": last_message.get("id"),
                "action": action,
                "reason": reason,
                "reply_text": reply_text,
                "model": self.config.openrouter.model,
                "raw_response": raw_response,
                "reasoning_details": reasoning_details or [],
                "classifier_category": classifier_category,
                "classifier_reason": classifier_reason,
                "classifier_confidence": classifier_confidence,
                "classifier_model": (
                    self.config.classifier.openrouter.model
                    if self.classifier_llm is not None
                    and classifier_raw_response is not None
                    else None
                ),
                "classifier_raw_response": classifier_raw_response,
            }
        )

    def _skip(
        self,
        negotiation: dict,
        reason: str,
        last_message: dict | None = None,
        *,
        persist: bool = True,
    ) -> None:
        self.stats.skipped += 1
        self._log_negotiation_outcome(
            negotiation,
            "skipped",
            reason=reason,
            last_message_id=(last_message or {}).get("id") or "-",
        )
        if (
            persist
            and last_message
            and self.config.force is False
            and reason != "already_processed"
        ):
            self._save_decision(
                negotiation=negotiation,
                last_message=last_message,
                action="skip",
                reason=reason,
                reply_text="",
                raw_response="",
                reasoning_details=[],
            )
