# Goal: MCP MVP for hh-applicant-tool

Date started: 2026-05-18
Status: active
Codex goal: `/goal Реализовать MCP MVP для hh-applicant-tool по docs/PRD.md и docs/goals/2026-05-18-mcp-mvp-goal.md: programmatic profile context, VacancyResearchService, audit persistence, stdio MCP tools, fail-closed destructive apply, tests and README updates, without breaking existing CLI behavior.`

## Objective

Implement the MCP MVP described in `docs/PRD.md` so an external MCP client can safely inspect HH profile data, research vacancies, analyze suitability, dry-run applications, and perform real applications only behind explicit safety gates.

Done when:

- `hh-applicant-mcp` starts over `stdio`.
- Request handling uses a non-CLI profile context, not `HHApplicantTool.__init__(argv)` or CLI operations.
- The MVP tools from `docs/PRD.md` are available: `hh_whoami`, `hh_list_resumes`, `hh_search_vacancies`, `hh_get_vacancy`, `hh_analyze_vacancy`, `hh_research_vacancies`, `hh_apply_vacancy`, `hh_research_and_apply`.
- Destructive tools default to `dry_run=true` and require server `allow_apply=true`, request `dry_run=false`, and request `confirm_apply=true` for real HH mutation.
- Analysis/apply decisions are audited in SQLite, dry-run does not write `vacancy_response_dedup`, and repeated real apply is blocked by relations, dedupe, and prior applied/unknown attempts.
- Existing CLI behavior and tests remain green.

## Scope

In scope:

- Add a programmatic profile/runtime context for config, sessions, cookies, storage and API client creation.
- Add `HHApplicantTool.from_profile(...)` as a compatibility shim only.
- Add a `VacancyResearchService` and supporting typed service modules.
- Add structured vacancy policy, analysis, cover-letter and safety logic for MCP flows.
- Add MCP audit tables, models and repositories.
- Add a `stdio` MCP server entrypoint and MVP tool handlers.
- Add focused tests for context, service logic, audit storage, MCP tools and stdio startup.
- Update README with MCP startup/config/policy examples after implementation is real.

Out of scope:

- Rewriting existing CLI commands during the MVP.
- Using CLI operations directly as MCP handlers.
- Streamable HTTP transport.
- Docker packaging for MCP.
- HH authorization tools inside MCP.
- CAPTCHA solving, HH test solving, remote vacancy blacklisting, SMTP email sending, or raw `call-api` through MCP.
- Multi-profile or multi-client server mode.

## Repository Context

- Design baseline: `docs/PRD.md`.
- Existing stage notes: `docs/Stage-0+4.md`, `docs/Stage-1.md`, `docs/Stage-2.md`, `docs/Stage-3a.md`, `docs/Stage-3b.md`, `docs/up.md`.
- CLI entrypoint and profile lifecycle: `src/hh_applicant_tool/main.py`.
- Current apply flow and dedupe algorithm: `src/hh_applicant_tool/operations/apply_vacancies.py`.
- API client and HH error mapping: `src/hh_applicant_tool/api/client.py`, `src/hh_applicant_tool/api/errors.py`.
- SQLite facade/schema/models/repositories: `src/hh_applicant_tool/storage/`.
- Structured OpenRouter client and chat-agent precedent: `hh_llm_agent/openrouter.py`, `hh_llm_agent/service.py`, `hh_llm_agent/gateway.py`.
- Current code already contains `openai_session`, `check_same_thread=False`, `skipped_vacancies`, AI vacancy filter, and `OpenRouterChatClient.complete_json()` support.
- Current code does not contain `src/hh_applicant_tool/context.py`, `src/hh_applicant_tool/services/`, `src/hh_applicant_tool/mcp/`, MCP audit models/repositories, or MCP tests.

## Implementation Plan

1. Programmatic context
   - Create `src/hh_applicant_tool/context.py` with `HHProfileContext`.
   - Preserve existing profile config rules, user-agent generation, cookies, proxy handling, `openai_session`, storage, API client and token/cookie persistence behavior.
   - Add `HHApplicantTool.from_profile(...)` without changing `HHApplicantTool.__init__(argv)`.
   - Add `tests/test_context.py`.

2. Service core
   - Add `src/hh_applicant_tool/services/types.py`, `policy.py`, `cover_letter.py`, `vacancy_research.py`.
   - Implement search, similar vacancies, vacancy details, dedupe key calculation and hard prechecks.
   - Keep service code free of `argparse`, `print()`, CLI exit codes, HTML test solving, remote blacklisting and SMTP.
   - Add service tests with fake API/storage/LLM.

3. Structured analysis and cover letters
   - Use `OpenRouterChatClient.complete_json()` for vacancy analysis.
   - Return `blocked` without LLM for hard prechecks.
   - Return `degraded` and `recommended_action="review"` on LLM failure or malformed JSON.
   - Implement cover-letter source precedence: provided text, provided template, policy/config template, LLM, none.

4. Audit persistence
   - Add `mcp_runs`, `vacancy_analysis`, and `application_attempts` to `schema.sql`.
   - Add matching models and repositories and wire them into `StorageFacade`.
   - Ensure dry-run persists audit but not `vacancy_response_dedup`.
   - Ensure real apply persists attempts and dedupe on success.

5. MCP stdio skeleton
   - Add `src/hh_applicant_tool/mcp/server.py`, `context.py`, `schemas.py`, `tools.py`.
   - Add `hh-applicant-mcp` entrypoint and required MCP SDK dependency.
   - Ensure stdout is reserved for MCP frames and logs go to stderr.
   - Save token/cookies after tool invocation.

6. MVP MCP tools
   - Implement all eight MVP tools on top of `VacancyResearchService`.
   - Normalize errors to the PRD error envelope.
   - Enforce `dry_run`, `confirm_apply`, `allow_apply`, run/day limits, relations, semantic dedupe and prior `unknown` attempt blocks.

7. Regression and docs
   - Run new MCP/service tests plus existing CLI regression tests.
   - Update README only after implemented behavior is available.

## Validation Contract

- Static checks: `ruff check . && pylint hh_llm_agent/ src/hh_applicant_tool/`
- Tests: `pytest`
- Build: `poetry build`
- CLI smoke: `python -m hh_applicant_tool --help`
- MCP smoke: `hh-applicant-mcp --help` and a subprocess stdio test that proves stdout contains only MCP messages.
- Done when every command above passes or any intentionally skipped command is documented with the exact blocker and accepted by the user.

## Commit Guidance

- Commit after completing a logical phase or checkpoint.
- One commit equals one completed meaningful unit of work.

## Decisions

- 2026-05-18: Use `docs/PRD.md` as the authoritative design baseline for the goal.
- 2026-05-18: Use `stdio` only for the MVP; Streamable HTTP remains out of scope.
- 2026-05-18: Keep a single profile per MCP server process.
- 2026-05-18: Do not reuse CLI operations as MCP handlers because they print human output and mix domain logic with side effects.
- 2026-05-18: Treat existing `skipped_vacancies` as legacy CLI state, not policy-aware MCP analysis cache.
- 2026-05-18: Store cover-letter audit as preview plus SHA256, not full letter logs.

## Progress Log

- 2026-05-18 23:00: Read `docs/PRD.md`, stage docs, `pyproject.toml`, `main.py`, `schema.sql`, and repository file layout. Confirmed upstream-port prerequisites are partly complete and MCP modules are not yet implemented.
- 2026-05-18 23:05: Created this goal document as the launch contract for future `/goal` work. Next checkpoint is Phase 1 implementation: `HHProfileContext` and `HHApplicantTool.from_profile(...)`.
- 2026-05-18 23:19: Started Phase 1. Added `src/hh_applicant_tool/context.py`, exported `HHProfileContext`, added `HHApplicantTool.from_profile(...)`, and covered profile path resolution, user-agent persistence, separate OpenAI proxy resolution, and non-CLI shim behavior in `tests/test_context.py`. Verified `pytest tests/test_context.py`, `pytest tests/test_api_client.py`, and `python -m hh_applicant_tool --help`; `ruff` is not available in the current environment (`ruff`, `poetry`, and `python -m ruff` are missing).
- 2026-05-18 23:23: Started Phase 2 service core. Added `src/hh_applicant_tool/services/` with `SearchFilters`, `VacancyPolicy`, `VacancyResearchService`, search/similar/detail methods, semantic vacancy dedupe key generation, and hard prechecks for archived/manual-form/test/relations/policy/work-format/dedupe blocks. Added `tests/test_vacancy_research_service.py` and `VacancyResponseDedupRepository.exists(...)`. Verified `pytest tests/test_vacancy_research_service.py`, `python -m compileall -q src/hh_applicant_tool/services tests/test_vacancy_research_service.py`, `git diff --check`, and full `pytest` (`70 passed`).
- 2026-05-18 23:28: Added Phase 4 audit persistence foundation: `mcp_runs`, `vacancy_analysis`, `application_attempts` schema blocks, models, repositories, facade wiring, policy canonical hash, and `VacancyResearchService.record_precheck_analysis(...)`. Added `tests/test_vacancy_audit_storage.py` and extended service tests for precheck audit rows, day-bucket applied counts, and applied/unknown retry blockers. Verified `pytest tests/test_vacancy_audit_storage.py tests/test_vacancy_research_service.py`, `python -m compileall -q src/hh_applicant_tool/storage src/hh_applicant_tool/services tests/test_vacancy_audit_storage.py tests/test_vacancy_research_service.py`, `git diff --check`, `python -m hh_applicant_tool --help`, and full `pytest` (`75 passed`); `python -m ruff ...` remains unavailable because `ruff` is not installed.
- 2026-05-18 23:33: Added Phase 3 service behavior: structured `analyze_vacancy(...)` with OpenRouter JSON schema, degraded `review` fallback, cover-letter request/result handling, and fail-closed `apply_vacancy(...)` with dry-run planning, server/confirm gates, daily cap, previous applied/unknown attempt checks, real `/negotiations` POST, dedupe persistence on success, and unknown network outcome audit. Extended `tests/test_vacancy_research_service.py` for LLM success/degraded analysis, dry-run without dedupe, real gate blocking, and real apply dedupe persistence. Verified `pytest tests/test_vacancy_research_service.py tests/test_vacancy_audit_storage.py`, `python -m compileall -q src/hh_applicant_tool/services tests/test_vacancy_research_service.py`, `git diff --check`, `python -m hh_applicant_tool --help`, and full `pytest` (`80 passed`); `python -m ruff ...` is still unavailable.
- 2026-05-18 23:44: Added Phase 5 MCP stdio skeleton and initial Phase 6 tool registration. Added `src/hh_applicant_tool/mcp/` with `FastMCP` server bootstrap, runtime/config creation, OpenRouter client resolver, request lifecycle auth flushing, normalized tool error envelopes, and all eight MVP tool names wired to service handlers. Added `hh-applicant-mcp` entrypoint and `mcp` dependency in `pyproject.toml`. Added `tests/test_mcp_tools.py` and `tests/test_mcp_stdio.py`; stdio smoke verifies the server starts and emits no stdout before protocol input. Verified `pytest tests/test_mcp_tools.py tests/test_mcp_stdio.py -q`, `python -m compileall -q src/hh_applicant_tool/mcp tests/test_mcp_tools.py tests/test_mcp_stdio.py`, `git diff --check`, `python -m hh_applicant_tool.mcp.server --help`, `python -m hh_applicant_tool --help`, and full `pytest` (`84 passed`). `python -m ruff ...` remains unavailable. Note: `poetry.lock` was not updated because `poetry` is unavailable in the current environment.
- 2026-05-19 00:05: Completed README/config/dependency validation checkpoint. Added README MCP startup/config/tools/audit/safety docs; added profile `mcp` and `vacancy_policy` config loading with optional policy file; made MCP handlers merge default policy with request overrides; updated research summary counts; added FastMCP end-to-end read-only tool test and config loading test. Updated `poetry.lock` with `mcp` dependency tree and bumped dev `pylint` to `>=3.3,<4.0` so the lint smoke works on the available Python 3.13. Fixed ruff findings in `debug-dump` and added the intended test `E402` ignore. Corrected the documented pylint path from nonexistent `src/hh_llm_agent/` to `hh_llm_agent/`.
- 2026-05-19 00:06: Verified checkpoint with `pytest` (`86 passed`), `PYTHONPATH=/tmp/codex-poetry python -m poetry run pytest` (`86 passed`), `PYTHONPATH=/tmp/codex-poetry python -m poetry run ruff check .` (pass, deprecation warning only), `PYTHONPATH=/tmp/codex-poetry python -m poetry run pylint hh_llm_agent/ src/hh_applicant_tool/` (10.00/10), `PYTHONPATH=/tmp/codex-poetry python -m poetry check --lock` (pass, Poetry deprecation warnings only), `python -m build` (pass after approved network access for isolated `poetry-core` install), `PYTHONPATH=/tmp/codex-poetry python -m poetry run hh-applicant-mcp --help`, `PYTHONPATH=/tmp/codex-poetry python -m poetry run hh-applicant-tool --help`, `python -m hh_applicant_tool.mcp.server --help`, and `git diff --check`.
- 2026-05-19 00:12: Closed the request-timeout gap from `docs/PRD.md`: added optional `BaseClient.timeout`, passed it into `requests.Session.request(...)`, propagated it to `OAuthClient`, and made MCP runtime apply `MCPServerConfig.request_timeout_seconds` to the HH `ApiClient`. Added tests for API timeout forwarding and MCP runtime timeout application. Verified `PYTHONPATH=/tmp/codex-poetry python -m poetry run pytest` (`88 passed`), `PYTHONPATH=/tmp/codex-poetry python -m poetry run ruff check .`, `PYLINTHOME=/tmp/pylint PYTHONPATH=/tmp/codex-poetry python -m poetry run pylint hh_llm_agent/ src/hh_applicant_tool/`, `python -m build`, `PYTHONPATH=/tmp/codex-poetry python -m poetry run hh-applicant-mcp --help`, `PYTHONPATH=/tmp/codex-poetry python -m poetry run hh-applicant-tool --help`, and `git diff --check`.

## Risks and Blockers

- Protocol-level stdio client initialization was not used as a final gate in this environment because previous attempts hung; current coverage proves server startup, stdout silence before protocol input, tool registration, and in-process FastMCP tool invocation.
- `POST /negotiations` network ambiguity needs careful `status="unknown"` handling to avoid duplicate applications.
- Existing CLI behavior must remain stable while service code is introduced additively.

## Final Verification

- Not complete yet. Implementation and validation checkpoints are in place, but final completion still needs a requirement-by-requirement audit against `docs/PRD.md` before calling the goal complete.
