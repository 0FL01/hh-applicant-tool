from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from hh_applicant_tool.api.errors import ApiError
from hh_applicant_tool.utils.date import parse_api_datetime

from .config import AgentConfig, load_agent_config
from .gateway import HHGateway
from .openrouter import LLMReply, OpenRouterChatClient, OpenRouterError

logger = logging.getLogger(__package__)


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
        self.stats = RunStats()
        self.run_id = uuid4().hex

    def run(self) -> RunStats:
        self._save_run(status="running")
        try:
            me = self.gateway.get_user()
            resumes = self._get_resume_map()
            blacklisted = (
                self.gateway.get_blacklisted()
                if self.config.skip_blacklisted
                else set()
            )

            for negotiation in self.gateway.get_negotiations():
                if self.config.limit and self.stats.total >= self.config.limit:
                    break
                self.stats.total += 1
                try:
                    self._process_negotiation(
                        negotiation=negotiation,
                        me=me,
                        resumes=resumes,
                        blacklisted=blacklisted,
                    )
                except (ApiError, OpenRouterError, ValueError) as ex:
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
            return self.stats
        except Exception:
            self._save_run(status="failed")
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
            m
            for m in self.gateway.fetch_messages(negotiation["id"])
            if m.get("text")
        ]
        if not messages:
            self._skip(negotiation, reason="no_messages")
            return

        self._save_messages(negotiation, messages)

        last_message = messages[-1]
        if last_message["author"]["participant_type"] != "employer":
            self._skip(
                negotiation,
                reason="last_message_not_from_employer",
                last_message=last_message,
            )
            return

        if not self.config.force and self._decision_exists(
            negotiation_id=negotiation["id"],
            last_message_id=last_message["id"],
        ):
            self._skip(
                negotiation,
                reason="already_processed",
                last_message=last_message,
            )
            return

        reply = self.llm.complete_json(
            self._build_llm_messages(
                negotiation=negotiation,
                resume=resume,
                me=me,
                messages=messages,
            )
        )
        decision = self._normalize_decision(reply)
        self._save_decision(
            negotiation=negotiation,
            last_message=last_message,
            action=decision["action"],
            reason=decision["reason"],
            reply_text=decision["reply_text"],
            raw_response=reply.content,
            reasoning_details=reply.reasoning_details,
        )

        if decision["action"] != "reply" or not decision["reply_text"]:
            self.stats.skipped += 1
            print(
                f"⏭️ Пропущен чат {negotiation['id']}: {decision['reason'] or 'model_skip'}"
            )
            return

        if self.config.dry_run:
            self.stats.replied += 1
            print(
                f"🧪 dry-run чат {negotiation['id']}: {decision['reply_text']}"
            )
            return

        self.gateway.send_message(negotiation["id"], decision["reply_text"])
        self.stats.replied += 1
        print(
            f"📨 Ответ отправлен для {negotiation['vacancy']['alternate_url']}"
        )

    def _build_llm_messages(
        self,
        negotiation: dict,
        resume: dict,
        me: dict,
        messages: list[dict],
    ) -> list[dict[str, str]]:
        vacancy = negotiation.get("vacancy") or {}
        employer = vacancy.get("employer") or {}
        history = messages[-self.config.max_history_messages :]

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
                    f"- Отвечать нужно от лица кандидата."
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
            {"role": "user", "content": self.config.reply_instruction}
        )
        return llm_messages

    def _normalize_decision(self, reply: LLMReply) -> dict[str, str]:
        payload = reply.parsed or {}
        action = str(payload.get("action") or "skip").strip().lower()
        if action not in {"reply", "skip"}:
            action = "skip"
        reply_text = str(payload.get("reply_text") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if action == "reply" and not reply_text:
            action = "skip"
            reason = reason or "empty_reply"
        return {
            "action": action,
            "reply_text": reply_text,
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

    def _save_decision(
        self,
        negotiation: dict,
        last_message: dict,
        action: str,
        reason: str,
        reply_text: str,
        raw_response: str,
        reasoning_details,
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
            }
        )

    def _skip(
        self,
        negotiation: dict,
        reason: str,
        last_message: dict | None = None,
    ) -> None:
        self.stats.skipped += 1
        if (
            last_message
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
