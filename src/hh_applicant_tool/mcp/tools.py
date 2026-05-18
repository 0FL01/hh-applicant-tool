from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass
from functools import wraps
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from hh_applicant_tool.services import VacancySearchResult
from hh_applicant_tool.storage.models.mcp_run import MCPRunModel
from hh_applicant_tool.utils import json

from .context import MCPRuntime
from .schemas import parse_cover_letter, parse_filters, parse_policy


class MCPToolHandlers:
    def __init__(self, runtime: MCPRuntime):
        self.runtime = runtime

    def hh_whoami(self, refresh: bool = False) -> dict[str, Any]:
        run_id = self._start_run("hh_whoami", dry_run=True)
        user = self.runtime.profile.get_me()
        self._finish_run(run_id, status="completed")
        return {
            "run_id": run_id,
            "profile_id": self.runtime.profile_id,
            "authenticated": True,
            "user": {
                "id": str(user.get("id")),
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
                "middle_name": user.get("middle_name"),
                "email": user.get("email"),
                "phone": user.get("phone"),
                "counters": user.get("counters", {}),
            },
        }

    def hh_list_resumes(
        self,
        only_selected: bool = False,
        only_published: bool = False,
    ) -> dict[str, Any]:
        run_id = self._start_run("hh_list_resumes", dry_run=True)
        resumes = list(self.runtime.profile.get_resumes())
        if resumes:
            self.runtime.profile.storage.resumes.save_batch(resumes)
        self._finish_run(run_id, status="completed", total_candidates=len(resumes))
        return {
            "run_id": run_id,
            "resumes": [
                {
                    "id": resume.get("id"),
                    "title": resume.get("title"),
                    "status": (resume.get("status") or {}).get("id"),
                    "published": bool(resume.get("can_publish_or_update") is False),
                    "selected": False,
                    "alternate_url": resume.get("alternate_url"),
                }
                for resume in resumes
            ],
        }

    def hh_search_vacancies(
        self,
        source: str = "auto",
        resume_id: str | None = None,
        search: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = self._start_run(
            "hh_search_vacancies",
            dry_run=True,
            resume_id=resume_id,
            search_params={"source": source, "search": search, "filters": filters or {}},
        )
        result = self._search(
            source=source,
            resume_id=resume_id,
            search=search,
            filters=filters,
        )
        self._finish_run(
            run_id,
            status="completed",
            total_candidates=len(result.items),
        )
        return {"run_id": run_id, **_to_dict(result)}

    def hh_get_vacancy(self, vacancy_id: str) -> dict[str, Any]:
        run_id = self._start_run("hh_get_vacancy", dry_run=True)
        vacancy = self.runtime.service.get_vacancy_details(vacancy_id).vacancy
        self._finish_run(run_id, status="completed")
        return {"run_id": run_id, "vacancy": vacancy}

    def hh_analyze_vacancy(
        self,
        resume_id: str,
        vacancy_id: str,
        analysis_mode: str = "light",
        policy: dict[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        resolved_policy = parse_policy(policy)
        run_id = self._start_run(
            "hh_analyze_vacancy",
            dry_run=True,
            resume_id=resume_id,
            policy=resolved_policy.to_canonical_dict(),
            policy_hash=resolved_policy.hash(),
        )
        result = self.runtime.service.analyze_vacancy(
            resume_id=resume_id,
            vacancy_id=vacancy_id,
            analysis_mode=analysis_mode,
            policy=resolved_policy,
            run_id=run_id,
        )
        self._finish_run(
            run_id,
            status="completed",
            analyzed_count=1,
            skipped_count=1 if result.recommended_action == "skip" else 0,
            blocked_count=1 if result.analysis_status == "blocked" else 0,
        )
        return {"run_id": run_id, **asdict(result)}

    def hh_research_vacancies(
        self,
        resume_id: str,
        source: str = "auto",
        search: str | None = None,
        filters: dict[str, Any] | None = None,
        analysis_mode: str = "light",
        policy: dict[str, Any] | None = None,
        max_candidates: int = 20,
    ) -> dict[str, Any]:
        resolved_policy = parse_policy(policy)
        run_id = self._start_run(
            "hh_research_vacancies",
            dry_run=True,
            resume_id=resume_id,
            policy=resolved_policy.to_canonical_dict(),
            policy_hash=resolved_policy.hash(),
            search_params={"source": source, "search": search, "filters": filters or {}},
        )
        search_result = self._search(
            source=source,
            resume_id=resume_id,
            search=search,
            filters=filters,
        )
        results = []
        for vacancy in search_result.items[:max_candidates]:
            analysis = self.runtime.service.analyze_vacancy(
                resume_id=resume_id,
                vacancy_id=vacancy["id"],
                analysis_mode=analysis_mode,
                policy=resolved_policy,
                run_id=run_id,
                source=search_result.source,
                source_query=search,
            )
            results.append({"vacancy": vacancy, **_to_dict(analysis)})

        summary = self._analysis_summary(len(search_result.items), results)
        self._finish_run(run_id, status="completed", **summary)
        return {
            "run_id": run_id,
            "resume_id": resume_id,
            "source": search_result.source,
            "summary": self._public_research_summary(summary),
            "results": results,
        }

    def hh_apply_vacancy(
        self,
        resume_id: str,
        vacancy_id: str,
        analysis_id: str | None = None,
        analysis_mode: str = "light",
        policy: dict[str, Any] | None = None,
        cover_letter: dict[str, Any] | None = None,
        dry_run: bool = True,
        confirm_apply: bool = False,
        max_applications_per_run: int | None = None,
        max_applications_per_day: int | None = None,
    ) -> dict[str, Any]:
        resolved_policy = parse_policy(policy)
        run_id = self._start_run(
            "hh_apply_vacancy",
            dry_run=dry_run,
            confirm_apply=confirm_apply,
            resume_id=resume_id,
            policy=resolved_policy.to_canonical_dict(),
            policy_hash=resolved_policy.hash(),
        )
        result = self.runtime.service.apply_vacancy(
            resume_id=resume_id,
            vacancy_id=vacancy_id,
            analysis_id=analysis_id,
            analysis_mode=analysis_mode,
            policy=resolved_policy,
            cover_letter_request=parse_cover_letter(cover_letter),
            dry_run=dry_run,
            confirm_apply=confirm_apply,
            allow_apply=self.runtime.config.allow_apply,
            max_applications_per_day=(
                max_applications_per_day
                or self.runtime.config.max_applications_per_day
            ),
            run_id=run_id,
        )
        self._finish_run(
            run_id,
            status="completed",
            planned_apply_count=1 if result.status == "planned" else 0,
            applied_count=1 if result.status == "applied" else 0,
            blocked_count=1 if result.status == "blocked" else 0,
            error_count=1 if result.status in {"failed", "unknown"} else 0,
        )
        return {"run_id": run_id, **_to_dict(result)}

    def hh_research_and_apply(
        self,
        resume_id: str,
        source: str = "auto",
        search: str | None = None,
        filters: dict[str, Any] | None = None,
        analysis_mode: str = "light",
        policy: dict[str, Any] | None = None,
        cover_letter: dict[str, Any] | None = None,
        dry_run: bool = True,
        confirm_apply: bool = False,
        max_candidates: int = 20,
        max_applications_per_run: int | None = None,
        max_applications_per_day: int | None = None,
    ) -> dict[str, Any]:
        resolved_policy = parse_policy(policy)
        run_id = self._start_run(
            "hh_research_and_apply",
            dry_run=dry_run,
            confirm_apply=confirm_apply,
            resume_id=resume_id,
            policy=resolved_policy.to_canonical_dict(),
            policy_hash=resolved_policy.hash(),
            search_params={"source": source, "search": search, "filters": filters or {}},
        )
        search_result = self._search(
            source=source,
            resume_id=resume_id,
            search=search,
            filters=filters,
        )
        max_apply = (
            max_applications_per_run
            or self.runtime.config.max_applications_per_run
        )
        results = []
        applied_or_planned = 0
        for vacancy in search_result.items[:max_candidates]:
            if applied_or_planned >= max_apply:
                break
            attempt = self.runtime.service.apply_vacancy(
                resume_id=resume_id,
                vacancy_id=vacancy["id"],
                analysis_mode=analysis_mode,
                policy=resolved_policy,
                cover_letter_request=parse_cover_letter(cover_letter),
                dry_run=dry_run,
                confirm_apply=confirm_apply,
                allow_apply=self.runtime.config.allow_apply,
                max_applications_per_day=(
                    max_applications_per_day
                    or self.runtime.config.max_applications_per_day
                ),
                run_id=run_id,
            )
            if attempt.status in {"planned", "applied"}:
                applied_or_planned += 1
            results.append(_to_dict(attempt))

        summary = self._attempt_summary(len(search_result.items), results)
        self._finish_run(run_id, status="completed", **summary)
        return {
            "run_id": run_id,
            "dry_run": dry_run,
            "summary": self._public_attempt_summary(summary),
            "results": results,
        }

    def _search(
        self,
        *,
        source: str,
        resume_id: str | None,
        search: str | None,
        filters: dict[str, Any] | None,
    ) -> VacancySearchResult:
        resolved_filters = parse_filters(filters)
        if source == "search" or (source == "auto" and search):
            if not search:
                raise ValueError("search is required for source=search")
            return self.runtime.service.search_vacancies(
                search=search,
                filters=resolved_filters,
            )
        if not resume_id:
            raise ValueError("resume_id is required for similar vacancies")
        return self.runtime.service.get_similar_vacancies(
            resume_id=resume_id,
            filters=resolved_filters,
        )

    def _start_run(
        self,
        tool_name: str,
        *,
        dry_run: bool,
        confirm_apply: bool = False,
        resume_id: str | None = None,
        policy: dict[str, Any] | None = None,
        policy_hash: str = "default",
        search_params: dict[str, Any] | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        self.runtime.profile.storage.mcp_runs.save(
            MCPRunModel(
                id=run_id,
                tool_name=tool_name,
                status="running",
                transport=self.runtime.config.transport,
                profile_id=self.runtime.profile_id,
                resume_id=resume_id,
                dry_run=dry_run,
                confirm_apply=confirm_apply,
                policy_hash=policy_hash,
                policy_json=policy or {},
                search_params_json=search_params or {},
            )
        )
        return run_id

    def _finish_run(self, run_id: str, *, status: str, **counts: int) -> None:
        self.runtime.profile.storage.mcp_runs.finish(
            run_id,
            status=status,
            **counts,
        )

    def _analysis_summary(
        self,
        fetched: int,
        results: list[dict[str, Any]],
    ) -> dict[str, int]:
        return {
            "total_candidates": fetched,
            "analyzed_count": len(results),
            "planned_apply_count": 0,
            "applied_count": 0,
            "skipped_count": sum(
                1 for item in results if item["recommended_action"] == "skip"
            ),
            "blocked_count": sum(
                1 for item in results if item["analysis_status"] == "blocked"
            ),
            "error_count": 0,
        }

    def _attempt_summary(
        self,
        fetched: int,
        results: list[dict[str, Any]],
    ) -> dict[str, int]:
        return {
            "total_candidates": fetched,
            "analyzed_count": len(results),
            "planned_apply_count": sum(
                1 for item in results if item["status"] == "planned"
            ),
            "applied_count": sum(
                1 for item in results if item["status"] == "applied"
            ),
            "skipped_count": 0,
            "blocked_count": sum(
                1 for item in results if item["status"] == "blocked"
            ),
            "error_count": sum(
                1
                for item in results
                if item["status"] in {"failed", "unknown"}
            ),
        }

    def _public_research_summary(self, summary: dict[str, int]) -> dict[str, int]:
        return {
            "fetched": summary["total_candidates"],
            "analyzed": summary["analyzed_count"],
            "apply_recommended": 0,
            "review_recommended": 0,
            "skipped": summary["skipped_count"],
            "blocked": summary["blocked_count"],
            "errors": summary["error_count"],
        }

    def _public_attempt_summary(self, summary: dict[str, int]) -> dict[str, int]:
        return {
            "fetched": summary["total_candidates"],
            "analyzed": summary["analyzed_count"],
            "apply_recommended": 0,
            "planned": summary["planned_apply_count"],
            "applied": summary["applied_count"],
            "blocked": summary["blocked_count"],
            "failed": 0,
            "unknown": summary["error_count"],
        }


def register_tools(mcp: FastMCP, runtime: MCPRuntime) -> MCPToolHandlers:
    handlers = MCPToolHandlers(runtime)

    def wrap(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return fn(*args, **kwargs)
            except Exception as ex:
                raise ToolError(json.dumps(_error_envelope(ex))) from ex
            finally:
                runtime.flush_auth_state()

        return wrapped

    mcp.tool()(wrap(handlers.hh_whoami))
    mcp.tool()(wrap(handlers.hh_list_resumes))
    mcp.tool()(wrap(handlers.hh_search_vacancies))
    mcp.tool()(wrap(handlers.hh_get_vacancy))
    mcp.tool()(wrap(handlers.hh_analyze_vacancy))
    mcp.tool()(wrap(handlers.hh_research_vacancies))
    mcp.tool()(wrap(handlers.hh_apply_vacancy))
    mcp.tool()(wrap(handlers.hh_research_and_apply))
    return handlers


def _error_envelope(ex: Exception) -> dict[str, Any]:
    return {
        "code": "invalid_request" if isinstance(ex, ValueError) else "tool_error",
        "message": str(ex),
        "retryable": False,
        "details": {},
    }


def _to_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return dict(value)
