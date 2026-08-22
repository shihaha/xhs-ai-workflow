# 08 — Backend Requirements

- Version: research baseline v1
- Date: 2026-08-23
- Status: architecture requirements for candidate evaluation and later implementation

## 1. Backend mission

The next backend must combine two properties that are often confused:

1. **deterministic business correctness** — evidence, phase gates, state transitions, idempotency, immutable audit facts;
2. **bounded agent orchestration** — model chooses among allowed next actions using a controlled Tool Runtime.

The Agent layer must not replace the domain core.

## 2. Preferred top-level architecture

```text
Workbench UI
      │
      ▼
FastAPI / Workbench API
      │
      ├──────── Project / Human Action / Timeline queries
      │
      ▼
Agent Runtime
  ContextBuilder
  ToolRegistry
  PermissionPolicy
  Budget/Checkpoint/Event layer
      │
      ▼
Domain Services
  Radar / XHS / Shops / Analysis / Opportunity
  Product Definition / Build Handoff / Content (as authorized)
      │
      ├──────── Adapter Registry
      │             └─ Qianfan / CDP / CLI / Android / Bailian / MCP later
      │
      ▼
SQLite authoritative business state
      │
      ▼
Managed runtime files / evidence / manifests / hashes
```

## 3. Keep FastAPI + Python unless a measured requirement disproves it

The current backend is already Python 3.12, FastAPI, SQLAlchemy, Pydantic and SQLite. Pydantic-based typed Agent libraries also fit this stack naturally.

A next-gen framework must not force a language/server rewrite without demonstrating a concrete benefit that exceeds migration risk.

Open-source UI components may be TypeScript/React; the backend contract should isolate that difference.

## 4. Keep SQLite for V1 authoritative state

Current single-machine/single-user requirements do not justify a database-server migration by themselves.

SQLite should remain authoritative for V1 if it can safely support:

- Jobs;
- AgentRuns/Steps;
- Project/stage references;
- permission/human-action records;
- event/timeline projections;
- evidence relationships;
- current domain records.

A later ADR may change storage if concurrency/scale requirements materially change.

## 5. Split persistence responsibilities without discarding integrity rules

`backend/app/db.py` is currently very large because it contains engine lifecycle, migrations, integrity triggers and many feature-specific constraints.

Future structure may separate responsibilities such as:

```text
backend/app/persistence/
  engine.py
  session.py
  migrations/
  integrity/
  repositories/
```

But migration must preserve existing DB-level constraints/triggers where they protect important invariants.

Do not replace a proven DB invariant with “the Agent should remember not to do that.”

## 6. Authoritative domain records vs runtime records

### Domain-authoritative examples

- ranking snapshot/item;
- XHS profile/note evidence;
- shop discovery/result;
- artifact/hash/manifest binding;
- Analysis;
- Opportunity;
- human Opportunity review;
- future Product Definition;
- future Finished Product/UAT;
- Content revision/package facts.

### Runtime/operational examples

- AgentRun;
- AgentStep;
- PermissionDecision;
- HumanAction request;
- RunEvent;
- compact context projection;
- checkpoint;
- model/tool usage.

Runtime records may reference domain records. They must not silently become a second competing source of business truth.

## 7. Proposed new persistence tables

Names are provisional; semantics are required.

### `projects`

Purpose: top-level business journey.

Potential fields:

```text
id
name
current_stage
authorized_transition?
created_at
updated_at
```

It should reference domain entities rather than duplicate all of their fields.

### `agent_runs`

Fields defined in `06_AGENT_RUNTIME_REQUIREMENTS.md`.

### `agent_steps`

Append/immutable-after-completion where practical. Structured operational trace.

### `permission_decisions`

Bind decision to run/step/tool/args digest and reason/source.

### `human_actions`

Pending/resolved operator tasks with target object, action kind, allowed responses, decision actor/time and resolution metadata.

### `run_events` (optional separate projection)

If AgentStep alone is insufficient for streaming/UI, maintain an append-only event table. Events must be reconstructable from authoritative state or clearly operational.

### `agent_checkpoints`

May be explicit table or sealed checkpoint rows in AgentStep. Must reference last proven domain/tool outputs and budget state.

## 8. Product Definition must become a real domain concept before automatic B/C orchestration

Current code has `ProductRecord`, but current business architecture requires:

```text
approved Opportunity
 -> Product Research
 -> candidate Product Definition
 -> human Product Definition approval
 -> Product Build
```

The backend needs a durable Product Definition entity/lifecycle before a generalized Agent can safely automate this transition.

Minimum semantic fields:

```text
target_user
core_problem_or_purchase_motivation
use_scenarios
product_form
modules_or_functions
deliverables
differentiation_with_evidence
out_of_scope
risks/copyright/platform_constraints
supporting_research/evidence refs
review_status
reviewed_at
human decision
```

An Agent may draft/revise; only a human gate can authorize build.

## 9. Domain Tool layer

Agent tools should be thin, explicit facades over domain services.

Example:

```python
class ShopEvidenceSampleTool:
    def availability(...): ...
    def permission(...): ...
    def execute(payload):
        return shop_service.queue_evidence_sample(...)
```

Tool implementations must not duplicate the underlying business contract.

### Bad pattern

```text
Agent tool performs raw SQL + raw ADB + writes status itself
```

### Good pattern

```text
Agent tool validates typed request
 -> invokes hardened domain service
 -> receives typed durable result/ref
 -> records AgentStep/evidence refs
```

## 10. Composition root / dependency injection

`main.py` currently constructs many services/adapters directly. Next-gen should keep one explicit trusted composition root, but make runtime dependencies clear:

```text
Settings
Database
Domain repositories/services
Adapters + AdapterRegistry
ToolRegistry
PermissionPolicy
ContextBuilder
AgentRuntime
API routers
Event publisher
```

Test composition must be able to replace model/tools/adapters with deterministic fakes without changing production code paths.

## 11. Model abstraction

The Agent Runtime should depend on a provider-neutral model interface.

Minimum capabilities:

- structured action/output;
- usage reporting;
- timeout/retry categorization;
- model/provider identity;
- optional streaming;
- optional tool calling if the chosen runtime uses provider tools;
- no provider-specific business semantics.

Current business rule that production language-model calls go through approved Bailian configuration remains in force until explicitly changed.

Framework selection must not quietly route data to another provider.

## 12. Evidence boundary remains below the Agent

Before the model:

- resolve allowed evidence IDs;
- verify trust/current bytes/ownership/account scope as currently required;
- project compact facts.

After the model, before authoritative success:

- validate structured output;
- validate citations/ownership/account coverage;
- re-resolve evidence when current logic requires it;
- persist immutable snapshot/claim;
- commit with existing concurrency/integrity protections.

The Agent Runtime can orchestrate this service; it must not bypass the service's two-sided trust checks.

## 13. Transaction and CAS requirements

Where state can race, use compare-and-set or equivalent transaction predicates.

Examples:

- claim queued/waiting work;
- transition run state;
- resolve a HumanAction once;
- approve a pending Product Definition once;
- append/complete a step once;
- reserve idempotency key once;
- checkpoint only against expected current step/version.

A stale UI/model decision should return conflict rather than overwrite newer state.

## 14. Idempotency

Side-effecting tools need an idempotency strategy classified per tool.

Possible classes:

```text
pure/read-only
idempotent-by-natural-key
idempotent-by-request-key
non-replayable/external-uncertain
```

For non-replayable physical actions:

- do not claim exactly-once execution when it cannot be proven;
- persist the uncertainty;
- require human re-establishment of state;
- prefer new bounded attempts over replaying hidden history.

## 15. Event streaming

Backend should expose durable run state plus incremental events.

Preferred API shape:

```text
GET project/run/current state
GET run timeline (paged)
SSE/WebSocket subscribe from event cursor
POST explicit commands/decisions
```

Requirements:

- client can reconnect using last event cursor/time/ID;
- missed live events can be re-read from durable storage;
- stream loss never changes business state;
- event ordering per run is stable;
- sensitive fields are redacted before transport.

## 16. API command/query separation

Use explicit commands for meaningful transitions rather than generic record patches.

Examples:

```text
POST /opportunities/{id}/review
POST /product-definitions/{id}/review
POST /agent-runs/{id}/resume
POST /agent-runs/{id}/cancel
POST /human-actions/{id}/resolve
```

Avoid:

```text
PATCH object { status: "approved" }
```

when the transition requires domain validation.

## 17. Human-action trust model

A human-action resolution must be server-validated against:

- action is still pending;
- target/version still current;
- submitted resolution is one of allowed choices;
- required evidence/prerequisites still hold;
- caller/actor identity available at least as local operator identity in V1;
- resolution is persisted before dependent actions proceed.

Do not trust client-supplied conversation history as proof of prior approval.

## 18. Context projection storage

Context summaries/projections may be cached for cost/performance but must include:

```text
projection version
source evidence/domain IDs
source digest/fingerprint
created_at
model/algorithm if generated
trust = non_authoritative_projection
```

On evidence/fingerprint mismatch, recompute or invalidate.

Do not store projections in fields that appear indistinguishable from raw evidence.

## 19. Large-result handling

Backend tools should support returning:

```text
summary + typed rows + cursor/ref
```

instead of multi-megabyte JSON in every model turn.

Large result storage must:

- live under managed runtime root;
- have size limits;
- use stable references;
- optionally hash bytes;
- be safe from path traversal/symlink escape;
- have cleanup/retention semantics;
- remain queryable in chunks.

Existing artifact-hardening code is a valuable reference/asset.

## 20. Secrets

- `.env`, cookies, browser profiles, API keys and auth tokens never enter Git;
- secret values are not written into AgentStep/Event payloads;
- model-visible secrets should be minimized;
- tool subprocess environments are scoped;
- logs redact likely credentials;
- UI receives configured/unconfigured/ready status, not secret material.

## 21. Physical device/browser safety

Backend must keep platform automation behind adapters/services and retain:

- health check;
- login/captcha/layout detection;
- screenshots/UI hierarchy evidence where applicable;
- bounded actions;
- pause/cancel/human takeover;
- explicit `needs_human` on unsafe interruption;
- no automatic login bypass;
- no automatic unsafe replay after restart.

Agent orchestration does not relax these rules.

## 22. Worker model

Do not add a general distributed queue in the first implementation slice unless required.

Initial approach can use current local-process/worker patterns plus durable SQLite state.

A future queue/durability system becomes justified when requirements include:

- multiple machines;
- concurrent users;
- large parallel task volume;
- long-lived distributed execution;
- operational need that current Job/worker semantics cannot meet.

## 23. Test architecture

Must support layers:

### Unit

- Tool Registry;
- PermissionPolicy;
- ContextBuilder;
- budgets;
- Agent state transitions;
- checkpoint/idempotency;
- Product Definition gate.

### Runtime integration with fakes

- fake model emits structured actions;
- fake tools return success/failure/timeouts;
- pause/approve/resume;
- crash/restart checkpoint replay boundaries;
- large-output/context control;
- stuck/budget exhaustion.

### Domain parity

Run existing Radar/XHS/Shop/Analysis/Opportunity tests unchanged where possible.

### Controlled end-to-end

Fresh temporary SQLite + fake adapters/model, frontend through Project/Run/HumanAction flow.

### Real UAT

Kept separately from controlled tests for Qianfan/XHS/Android/Bailian; never infer live success from mocks.

## 24. Migration strategy

Do not rewrite all services to fit a framework.

Preferred sequence:

1. freeze/rerun current baseline;
2. add runtime persistence/tables without changing domain behavior;
3. implement fake-model/fake-tool runtime;
4. wrap one safe read-only domain workflow as tools;
5. compare results with existing direct service path;
6. add context optimization benchmark;
7. add human-gated orchestration;
8. only then consider physical collection integration;
9. migrate UI shell after backend run/event contract is stable enough.

## 25. Open-source backend/runtime candidate scorecard

Phase 3 candidates should be scored on:

1. license;
2. current maintenance;
3. Python/Pydantic/FastAPI fit;
4. typed tools/actions;
5. HITL/deferred execution;
6. checkpoints/durability;
7. event streaming;
8. hooks/middleware;
9. token/usage limits;
10. context/tool-output control;
11. provider independence;
12. SQLite/local compatibility;
13. ability to use our domain services without rewriting them;
14. ability to enforce custom PermissionPolicy;
15. Windows/local operation;
16. testability;
17. upgrade/removal cost;
18. operational complexity.

## 26. Backend acceptance statement

A new backend architecture is an improvement only if it can make the system more agentic **while preserving or strengthening every proven evidence, audit, failure and human-authority invariant**.

Cleaner abstractions do not compensate for weaker business truth.