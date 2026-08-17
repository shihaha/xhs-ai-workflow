# Xiaohongshu Intelligence Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a truthful Windows-local workbench that carries real evidence from controlled collection through AI analysis and reviewed content-package export.

**Architecture:** A React client consumes a versioned FastAPI API backed by SQLite. Long-running work is represented by durable database jobs; browser, Android and model integrations are replaceable adapters that produce normalized evidence-linked DTOs.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic 2, pytest, React 19, TypeScript, Vite, Vitest, Playwright, adbutils, uiautomator2, SQLite WAL.

**Spec:** `SYSTEM_SPEC_AND_ACCEPTANCE.md`

## Global Constraints

- Run on Windows from `D:\AI_WORKSPACE\xhs-intelligence-workbench` and store mutable runtime data under `D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench`.
- Do not insert demo business data into a fresh database.
- UI state must come from persisted facts; never simulate progress or success.
- Task states are exactly `queued`, `running`, `needs_human`, `succeeded`, `failed`, `cancelled`.
- External integrations must be replaceable adapters and preserve raw evidence.
- V1 never publishes, likes, favorites or comments on Xiaohongshu.
- Missing credentials, login or hardware is a visible configuration state, not a successful result.
- New behavior follows red-green-refactor; each task report records the observed failing and passing commands.

---

### Task 1: Repository foundation and truthful health API

**Files:**
- Create: `pyproject.toml`, `.env.example`, `README.md`
- Create: `backend/app/main.py`, `backend/app/settings.py`, `backend/app/api/health.py`
- Test: `backend/tests/test_health.py`, `backend/tests/test_settings.py`

**Interfaces:**
- Produces: `create_app() -> FastAPI`; `GET /api/v1/health`; `Settings.runtime_dir: Path`.

- [ ] Write a failing test proving a temporary runtime directory is created and health reports real checks for database path, ADB executable, browser support and Bailian configuration.
- [ ] Run `python -m pytest backend/tests/test_settings.py backend/tests/test_health.py -v` and confirm failure because the application does not exist.
- [ ] Implement the minimal settings and health endpoint; no external check may be hard-coded healthy.
- [ ] Re-run the focused tests and confirm pass.
- [ ] Run `python -m pytest -q` and commit with `feat: establish truthful backend health`.

### Task 2: Durable jobs, logs and evidence

**Files:**
- Create: `backend/app/db.py`, `backend/app/models/jobs.py`, `backend/app/schemas/jobs.py`
- Create: `backend/app/services/jobs.py`, `backend/app/api/jobs.py`
- Test: `backend/tests/test_jobs_api.py`, `backend/tests/test_job_state_machine.py`

**Interfaces:**
- Consumes: `Settings.runtime_dir`.
- Produces: `JobService.create`, `claim`, `transition`, `append_log`, `attach_artifact`; `/api/v1/jobs` CRUD/read endpoints.

- [ ] Write failing table-driven tests for every permitted and forbidden transition among the six fixed states.
- [ ] Write a failing API test proving job progress, logs and evidence are persisted and returned after a new application instance opens the same SQLite file.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement SQLAlchemy models, SQLite WAL initialization, the state machine and API.
- [ ] Add recovery behavior: expired `running` jobs become `needs_human` with a recovery log.
- [ ] Re-run focused tests, then the full backend suite, and commit with `feat: add durable jobs and evidence`.

### Task 3: Adapter contracts and source-repository probes

**Files:**
- Create: `backend/app/adapters/contracts.py`, `backend/app/adapters/registry.py`
- Create: `tools/probe_xhs_adapter.py`, `docs/THIRD_PARTY_EVALUATION.md`
- Test: `backend/tests/test_adapter_contracts.py`, `backend/tests/test_adapter_registry.py`, `backend/tests/test_probe_cli.py`

**Interfaces:**
- Produces: normalized `CollectionRequest`, `CollectionItem`, `CollectionResult`, `DeviceHealth`, `ModelResult`; registry lookup by capability and priority.

- [ ] Write failing contract tests proving invalid items without source URL/raw evidence are rejected and adapters cannot claim N/N when successful item count differs from expected count.
- [ ] Write a failing CLI test proving an unavailable candidate returns `unavailable` rather than success.
- [ ] Implement contracts, registry and a read-only probe command for `xiaohongshu-mcp`, `xhs-cli` and MediaCrawler.
- [ ] Run probes on this Windows machine and record observed commands/results in `docs/THIRD_PARTY_EVALUATION.md`; do not mark login-dependent capabilities verified without a real login.
- [ ] Run tests and commit with `feat: define replaceable collection adapters`.

### Task 4: React shell with fact-backed job screens

**Files:**
- Create: `frontend/package.json`, `frontend/vite.config.ts`, `frontend/src/main.tsx`
- Create: `frontend/src/api/client.ts`, `frontend/src/pages/SystemStatusPage.tsx`, `frontend/src/pages/JobsPage.tsx`
- Test: `frontend/src/pages/SystemStatusPage.test.tsx`, `frontend/src/pages/JobsPage.test.tsx`

**Interfaces:**
- Consumes: `/api/v1/health`, `/api/v1/jobs`.
- Produces: responsive local dashboard routes `/status` and `/jobs`.

- [ ] Write failing component tests for empty, loading, failed, `needs_human` and evidence-linked job states using complete API response fixtures.
- [ ] Run `npm test -- --run` and confirm expected failures.
- [ ] Implement the application shell and API-backed screens without timers that invent progress.
- [ ] Run tests and `npm run build`, then commit with `feat: add fact-backed workbench shell`.

### Task 5: Ranking ingestion, scoring and candidate accounts

**Files:**
- Create: `backend/app/features/radar/models.py`, `scoring.py`, `service.py`, `api.py`
- Create: `backend/app/adapters/qianfan_playwright.py`
- Test: `backend/tests/radar/test_scoring.py`, `test_rank_ingestion.py`, `test_candidate_api.py`

**Interfaces:**
- Consumes: job/evidence services and collection contracts.
- Produces: `/api/v1/radar/rank-snapshots`, `/api/v1/radar/accounts`, `/api/v1/radar/candidates`; explainable score components.

- [ ] Convert the tutorial scoring examples into literal failing fixtures and verify the current missing implementation fails.
- [ ] Add failing tests for eight-board snapshot uniqueness, source retention and deterministic deduplication.
- [ ] Implement models, scoring, ingestion service and API.
- [ ] Implement a controlled Playwright adapter that records raw page/response evidence and stops at login or layout changes with `needs_human`.
- [ ] Run focused and full backend tests; commit with `feat: add ranking radar and account scoring`.

### Task 6: Android shop collection and N/N verification

**Files:**
- Create: `backend/app/adapters/android_device.py`, `backend/app/features/shops/service.py`, `api.py`
- Test: `backend/tests/shops/test_device_health.py`, `test_shop_collection.py`, `test_nn_verification.py`

**Interfaces:**
- Consumes: normalized account, jobs and evidence.
- Produces: `/api/v1/devices`, `/api/v1/shop-collections`; explicit expected/discovered/succeeded/missing counts.

- [ ] Write failing tests for disconnected device, wrong foreground app, login prompt, changed selector, cancellation and partial N/N collection.
- [ ] Implement ADB/uiautomator2 health and a device adapter with selectors isolated in versioned profiles.
- [ ] Port reusable parsing and verification rules from tutorial attachments into tested Python modules.
- [ ] Ensure every screen transition can attach screenshot and UI hierarchy evidence.
- [ ] Run tests; if a real device is connected, run the opt-in smoke test, otherwise record `not_run: device unavailable`; commit with `feat: add evidence-backed Android shop collection`.

### Task 7: Bailian model adapter, analysis and opportunities

**Files:**
- Create: `backend/app/adapters/bailian.py`, `backend/app/features/analysis/schemas.py`, `service.py`, `api.py`
- Test: `backend/tests/analysis/test_structured_output.py`, `test_evidence_grounding.py`, `test_model_failures.py`

**Interfaces:**
- Consumes: account, note, product and evidence IDs.
- Produces: `/api/v1/analyses`, `/api/v1/opportunities`; persisted prompt version, usage and evidence citations.

- [ ] Write failing tests for malformed JSON, missing evidence IDs, rate limits, timeouts and retry exhaustion.
- [ ] Implement the OpenAI-compatible Bailian adapter and Pydantic validation; secrets remain environment-only.
- [ ] Implement account reports, product clustering and opportunity cards that reject uncited claims.
- [ ] Add opt-in live contract test guarded by `BAILIAN_API_KEY`; skipped is not reported as live-verified.
- [ ] Run tests and commit with `feat: add grounded Bailian analysis`.

### Task 8: Product and content workflow

**Files:**
- Create: `backend/app/features/content/models.py`, `schemas.py`, `service.py`, `api.py`, `export.py`
- Test: `backend/tests/content/test_workflow.py`, `test_review.py`, `test_export.py`

**Interfaces:**
- Consumes: opportunity and evidence IDs plus model adapter.
- Produces: `/api/v1/products`, `/api/v1/content-items`, `/api/v1/content-packages`; immutable revisions and review decisions.

- [ ] Write failing tests for product material versions, source-linked research, draft revision history, reject/regenerate/approve transitions and export blocking before approval.
- [ ] Implement product, keyword, research, template, draft and review persistence.
- [ ] Import useful tutorial templates/prompts as versioned seed configuration, never as completed business records.
- [ ] Implement deterministic ZIP export containing manifest, text, images, sources and review history.
- [ ] Run tests and commit with `feat: add reviewed content production workflow`.

### Task 9: Complete dashboard flows

**Files:**
- Create: `frontend/src/pages/RadarPage.tsx`, `AccountPage.tsx`, `OpportunitiesPage.tsx`, `ContentStudioPage.tsx`
- Modify: `frontend/src/main.tsx`, `frontend/src/api/client.ts`
- Test: corresponding `*.test.tsx`; `frontend/e2e/empty-to-package.spec.ts`

**Interfaces:**
- Consumes: all `/api/v1` feature APIs.
- Produces: operable navigation from collection through approved export.

- [ ] Write failing page tests for real empty/error/human-intervention states and an E2E test using a temporary backend/database with controlled adapter fixtures.
- [ ] Implement the smallest UI that exposes every accepted action, evidence link and failure recovery path.
- [ ] Run Vitest, frontend build and Playwright E2E; commit with `feat: complete workbench operator flows`.

### Task 10: Recovery, security and release verification

**Files:**
- Create: `backend/tests/integration/test_recovery_matrix.py`, `scripts/run-local.ps1`, `scripts/verify.ps1`
- Create: `docs/RUNBOOK.md`, `docs/UAT_CHECKLIST.md`, `docs/IMPLEMENTATION_STATUS.md`

**Interfaces:**
- Produces: one local start command, one full verification command, explicit live-UAT checklist and evidence locations.

- [ ] Write failing integration scenarios for lost cookies, network failure, device disconnect, changed selector, model throttling and process restart.
- [ ] Implement bounded retries, secret redaction, cancellation checkpoints and recovery guidance needed by those scenarios.
- [ ] Run backend tests, frontend tests/build, E2E and secret scan through `scripts/verify.ps1`.
- [ ] Start from a fresh runtime directory and verify no demo records appear.
- [ ] Record which real integrations were actually exercised; leave unavailable live checks unpassed.
- [ ] Commit with `chore: add release verification and UAT runbook`.

