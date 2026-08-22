# 02 — Current System Architecture

- Audit date: 2026-08-23
- Baseline: `feature/system-v1`
- Research copy: `research/xhs-workbench-next`

## 1. Architectural classification

The current system is best described as a **Windows-local modular monolith**:

- FastAPI application and composition root;
- SQLAlchemy + SQLite as the only business database;
- feature modules for radar, XHS collection, shops, analysis, content and media;
- explicit adapter boundary for external systems;
- durable job/evidence infrastructure;
- React/Vite operator UI;
- runtime files outside Git.

It is not currently a generic Agent Runtime. Business services call one another explicitly and make bounded model requests. There is no central autonomous model → tool → result loop spanning the application.

## 2. Technology stack

Backend:

- Python 3.12+
- FastAPI
- SQLAlchemy 2.x
- Pydantic / pydantic-settings
- Playwright
- adbutils + uiautomator2
- Pillow
- Uvicorn

Frontend:

- React 19
- TypeScript
- Vite
- Vitest
- Playwright E2E

Runtime/data:

- SQLite
- local runtime artifacts on NTFS
- persistent Chrome profile for authenticated collection
- one Android device in V1
- Alibaba Bailian for text/vision/image model calls

## 3. Composition root

`backend/app/main.py` wires the application services into `app.state`:

```text
Settings
  ├─ Database
  ├─ JobService
  ├─ AdapterRegistry
  │   └─ XHS read adapter
  ├─ XhsCollectionService
  ├─ RadarService
  ├─ QianfanCollectionService
  ├─ AndroidDeviceAdapter
  ├─ ShopCollectionService
  ├─ BailianModelAdapter
  ├─ AnalysisService
  ├─ ArtifactCleanupService / Worker
  ├─ ContentService
  └─ ContentMediaService / Worker
```

Routers expose health, jobs, radar, shops, analysis, content, media and XHS APIs.

This is simple and appropriate for a single-user local application, but the `app.state` service-locator pattern and one large composition root will become harder to evolve if a general Agent Runtime, multiple projects and more execution roles are added.

## 4. Durable Job layer

`JobService` is one of the strongest existing architectural assets.

State graph:

```text
queued ─────→ running ─────→ succeeded
  │             │ ├────────→ failed
  │             │ ├────────→ cancelled
  │             │ └────────→ needs_human
  └────────────→ cancelled       │
                                 └── explicit claim/resume → running
```

Properties already implemented:

- transactional job creation and batch creation;
- compare-and-set claim/transition semantics;
- worker leases;
- retry counter on explicit resume from `needs_human`;
- structured progress/stage/error facts;
- append-only logs;
- artifact bindings;
- idempotent `attach_artifact_once`;
- path containment validation;
- careful handling of SQLite/NTFS non-atomic finalization;
- expired running jobs become `needs_human`;
- interrupted physical workers require explicit restart instead of automatic Android/browser replay.

A next-generation runtime should normally sit **above or alongside** this durable lifecycle, not replace it with an in-memory Agent loop.

## 5. Adapter architecture

`backend/app/adapters/contracts.py` already defines provider-neutral contracts.

Collection contracts model:

- capability and parameters;
- stable IDs;
- source URLs;
- raw evidence;
- accepted/rejected/missing items;
- expected vs observed count;
- exact completeness semantics.

The validators prevent false completeness: unknown totals cannot claim success, deficits must be accounted for, overflow fails, duplicates are explicit, and successful exact collections must actually reconcile.

Other contracts cover:

- device health;
- structured model requests/results;
- model failures;
- visual assessment;
- generated-image bytes, hash, MIME and dimensions.

`AdapterRegistry` selects implementations by capability and priority. XHS CLI is the fallback, and the CDP implementation can take higher priority for supported reads.

This pattern is already close to part of a future Tool layer. The future Agent Tool Registry should not discard these adapter contracts; it should wrap domain-safe operations that themselves use these adapters.

## 6. Radar and collection flow

Current Phase A flow is approximately:

```text
Qianfan collection
→ normalized rank facts
→ tutorial scoring
→ stable candidate order
→ low-cost business-scope prescreen
→ Android preflight
→ XHS profile/latest-note collection
→ Android evidence sample / shop evidence
→ evidence eligibility
→ cross-account analysis
→ pending Opportunity
→ human review
```

Key distinction:

- adapters speak to external systems;
- feature services enforce business rules;
- JobService owns durable execution state;
- SQLite/artifacts own truth after persistence.

A process-local return value is never sufficient proof of completion.

## 7. Evidence architecture

Evidence is not merely a JSON blob passed to the model. The system has multiple binding layers:

- SQLite business row identities;
- job identity;
- artifact identity;
- runtime-relative path;
- source identity/source URL;
- SHA-256 and size;
- manifests where applicable;
- account ownership;
- collection mode and eligibility;
- immutable snapshots for successful analysis.

The database also contains integrity triggers that prevent important evidence/review facts from being silently rewritten.

Examples include:

- immutable sealed analysis evidence snapshots;
- immutable Opportunity evidence facts;
- one-way Opportunity review transitions;
- Opportunity requiring a successful analysis;
- artifact-backed XHS account facts becoming immutable.

This evidence layer is a business asset and must remain stronger than any future LLM planning layer.

## 8. Analysis architecture

`AnalysisService.create()` is a deterministic guarded pipeline, not an autonomous agent.

Simplified sequence:

```text
resolve trusted evidence
→ calculate digest / eligibility
→ construct bounded structured-model request
→ call Bailian
→ Pydantic validate model output
→ enforce evidence grounding
→ enforce specific-shared-demand contract
→ derive candidate projection
→ BEGIN IMMEDIATE
→ re-resolve evidence after model call
→ compare trust fingerprint / scope / IDs
→ persist immutable evidence snapshot
→ re-check artifact bindings
→ commit analysis + Opportunity graph
→ classify uncertain commit if necessary
```

The review path again revalidates current evidence before an Opportunity can be approved.

This is unusually defensive and should be preserved conceptually.

### Current context construction weakness

The current analysis request serializes the full resolved `allowed_evidence` plus the full output JSON schema into the user prompt. The current real successful run reached 243,276 prompt tokens for two accounts / 22 evidence facts.

That makes Context Builder / evidence compaction one of the clearest next-generation optimization targets. Any compaction layer must preserve source IDs and may never upgrade weak evidence into trusted evidence.

## 9. Database architecture

SQLite is the single business database and is appropriate for V1 local/single-user use.

However, `backend/app/db.py` has grown to roughly 232 KB and contains lifecycle code, migrations, integrity triggers and evidence-related upgrade logic in one large module.

This is not a reason to switch to PostgreSQL. It is a reason to separate responsibilities if/when the next-generation backend is created, for example:

```text
db/
  engine.py
  migrations/
  integrity/
  repositories/
```

The important part to preserve is the database-enforced invariants, not the current single-file layout.

## 10. Content and media architecture

The repository already contains substantial `content` and `media` infrastructure:

- Product rows tied to an Opportunity;
- versioned managed Product materials;
- hash/size/type validation;
- cleanup/quarantine records;
- content item and revision lifecycle;
- model generation;
- human review with expected-revision CAS;
- regeneration after rejection;
- deterministic pending-publication package export;
- media generation/vision checks.

This is real code, but it must be interpreted carefully.

The current business handoff says an approved Opportunity only authorizes **product research**. It does not by itself define a concrete product. Therefore the technical ability to create a `ProductRecord` must not be mistaken for a complete `Approved Product Definition` business gate.

The existing content code should be audited for selective reuse after the B/C/D information model is finalized.

## 11. Frontend architecture

The current React frontend exposes:

- `/radar`
- `/accounts/:id`
- `/opportunities`
- `/content`
- `/status`
- `/jobs`

Routing is handled directly from `window.location.pathname`; there is no general router or generic workbench framework.

The UI is therefore a functional domain-specific operator interface, not an extensible Agent workbench shell. That is acceptable for the current Phase A, but next-generation requirements will likely include:

- project/run hierarchy;
- agent/tool timeline;
- human-action inbox;
- evidence viewer;
- token/cost/budget visibility;
- checkpoints/resume;
- B/C/D stage transitions.

Whether to extend the existing React UI or adopt a licensed open-source shell should be decided only after candidate evaluation.

## 12. Operations and recovery

The repository provides local start/verification scripts and a runbook.

Important runtime principles:

- code and runtime facts are separated;
- no credentials/browser profiles/phone evidence in Git;
- controlled adapter tests do not masquerade as live platform proof;
- login/captcha/layout/device problems become explicit human actions;
- model retry is bounded;
- abandoned work is recoverable but not silently resumed into success;
- uncertain filesystem/database ownership retains bytes for audit rather than deleting them optimistically.

## 13. Current architecture in one sentence

**The current system is a strong evidence-and-state backend with deterministic domain workflows and a modest operator UI; what it lacks is a reusable intelligent orchestration layer and a unified A→B→C→D workbench model, not basic backend correctness.**
