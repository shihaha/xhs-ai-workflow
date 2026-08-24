# 12 — Implementation Roadmap

- Date: 2026-08-23
- Status: implementation plan after research phases 1–3
- Principle: prove the new architecture in small protected slices; never mix framework adoption, domain migration and UI rewrite into one step

## 0. Before code: freeze the baseline

### Goal

Know exactly what the current system does before inserting Agent infrastructure.

### Actions

1. create an isolated implementation/spike branch from the current approved baseline/research point;
2. run the current backend verification suite;
3. run frontend Vitest + production build + controlled Playwright flow;
4. record existing green/red baseline instead of assuming all historical failures are fixed;
5. preserve representative read-only fixtures/references for:
   - Job state transitions;
   - trusted XHS/account evidence;
   - shop evidence sample;
   - successful/failed Analysis;
   - Opportunity review;
6. record the historical 243,276-token two-account/22-evidence case as a context benchmark.

### Exit gate

No Agent code starts until the baseline result is recorded. Existing unrelated failures may remain, but they must be explicitly classified.

---

# Stage 1 — Pure Agent Runtime spike

## Goal

Prove Pydantic AI/Harness is a good implementation library **without touching a real XHS workflow**.

## Proposed new package

```text
backend/app/agent_runtime/
  __init__.py
  types.py
  model.py
  tools.py
  permissions.py
  context.py
  events.py
  checkpoints.py
  runtime.py
```

## Persistence

Add minimal tables/models/repositories for:

```text
agent_runs
agent_steps
permission_decisions
human_actions
agent_checkpoints   # or sealed checkpoint steps
```

Do not change existing Job/Analysis/Opportunity rows to make the spike easier.

## Fake tools

Use deterministic tools such as:

```text
echo.read                 # read-only
counter.increment         # idempotency-test write
external.simulated_action # ask/unsafe test
large_result.read         # output/context test
failure.transient         # retry test
failure.permanent         # fail-closed test
```

## Required scenarios

1. model chooses valid read tool -> tool executes -> step persists;
2. malformed/unknown tool call -> rejected before execution;
3. unavailable tool -> cannot execute;
4. permission allow -> execute;
5. permission ask -> durable human wait;
6. permission deny -> durable denial/failure, no tool side effect;
7. human resolution -> explicit continuation;
8. max-step/model/token/wall-time budgets -> safe stop;
9. crash after committed step -> resume after checkpoint without duplicate write;
10. crash before uncertain side effect can be proven -> no false checkpoint;
11. large output -> bounded/spilled/compacted;
12. hidden chain-of-thought is not persisted.

## Exit gate

- all Stage 1 tests green;
- runtime code depends on project-owned Tool/Permission/Context interfaces, not Pydantic internals everywhere;
- existing domain tests unchanged and still at baseline.

If Pydantic AI/Harness fights these requirements, stop and evaluate LangGraph/MAF before domain integration.

---

# Stage 2 — Wrap one safe domain path

## Goal

Prove the Agent can use the real backend without weakening it.

## Tool candidates

Start with read-only operations only:

```text
radar.list_candidates
account.read_profile
account.read_latest_notes
opportunity.read
job.read
```

Do **not** start Android/Qianfan collection.

## Architecture

```text
Agent
 -> DomainTool
 -> existing service/query path
 -> typed result + evidence refs
 -> AgentStep
```

## Acceptance

- direct service/API result and Agent-tool result match semantically;
- Agent cannot access raw SQL or arbitrary files;
- evidence IDs survive tool projection;
- unavailable prerequisites produce typed unavailable state;
- existing pages/APIs behave exactly as baseline.

---

# Stage 3 — Context Builder benchmark

## Goal

Solve the concrete token problem before adding more autonomy.

## Build

Implement:

```text
ContextBuilder
EvidenceProjection
ToolSchemaSelector
ToolResultLimiter
ContextUsageReporter
```

## Context layers

```text
agent goal
relevant business rules
current Job/AgentRun
selected compact evidence facts
available relevant tool schemas
recent checkpoint summary
```

Raw evidence remains outside the prompt and fully authoritative.

## Benchmark

Replay the historical shape:

```text
2 accounts
22 eligible evidence facts
historical prompt tokens = 243,276
```

### Pass condition

- materially lower prompt-token usage;
- same evidence IDs available for grounding;
- same/current evidence revalidation remains mandatory;
- compact facts cannot become trusted evidence;
- output quality/reliability is not worse on controlled replay.

Do not optimize for a token number by deleting required evidence/business constraints.

---

# Stage 4 — Agent workbench UI spike

## Goal

Prove the new runtime can be operated through a real workbench rather than logs.

## Backend

Expose:

```text
GET /projects/{id}
GET /agent-runs
GET /agent-runs/{id}
GET /agent-runs/{id}/steps
GET /human-actions
POST /human-actions/{id}/resolve
POST /agent-runs/{id}/resume
POST /agent-runs/{id}/cancel
stream /agent-runs/{id}/events
```

Exact route names can change; command semantics cannot.

## UI

Keep the current React/Vite application.

Add minimum navigation:

```text
Project
Runs
Human Actions
Evidence / existing domain pages
System Status
```

Integrate an AG-UI-compatible stream/adapter and test selective CopilotKit components.

## UI acceptance

1. run starts and timeline streams;
2. tool/permission/checkpoint rows are visible;
3. human action stops run;
4. user resolves it;
5. run continues;
6. browser refresh reconstructs exact state from backend;
7. network/stream drop does not fabricate completion;
8. current Radar/Opportunity pages remain functional;
9. business decisions remain backend-owned.

### Fallback

If CopilotKit is awkward inside current Vite/domain shell, evaluate assistant-ui or build minimal custom components on the same backend/AG-UI contract. Do not rewrite backend because of a UI library.

---

# Stage 5 — Real read/analysis orchestration

## Goal

Let the Agent coordinate a useful but non-physical real workflow.

Potential flow:

```text
read Project/Opportunity
 -> read supporting account/evidence summaries
 -> check evidence completeness
 -> call bounded analysis/verification service
 -> present result / human action
```

The Agent should orchestrate existing domain services; the existing AnalysisService continues enforcing evidence grounding and success persistence.

## Acceptance

- no direct model output can create a trusted Opportunity;
- existing service revalidation still runs;
- usage/context/timeline visible;
- same direct-analysis business contract is preserved;
- repeated run is separately auditable.

---

# Stage 5.5 — Workbench usability + Chinese localization pass

## Goal

Before the workbench is allowed to drive real physical/browser collection, make the operator surface understandable to the actual user rather than leaving it as an engineering console.

This pass must **not** rewrite the backend lifecycle or delay Stage 5 correctness work. It is a dedicated UI/usability slice between Stage 5 and Stage 6.

## Chinese-first UI rule

The primary workbench language is Chinese.

All operator-facing content must be Chinese by default, including:

- page headings and section names;
- buttons and confirmation text;
- Job / AgentRun / HumanAction status labels;
- error and warning messages;
- capability/permission explanations;
- empty states and help text;
- timeline descriptions and action results.

Technical identities such as `Job`, `AgentRun`, `HumanAction`, `Evidence`, `Artifact`, tool names, IDs and raw backend enum values may remain visible where useful for debugging/audit, but they must not be the only explanation shown to the user. The UI should present a clear Chinese label/explanation alongside them.

Examples:

```text
Agent Runs -> Agent 执行记录
Human Actions -> 等待人工处理
Pending -> 等待处理
needs_human -> 需要人工处理
Grounded Agent analysis -> 基于证据的 Agent 分析
Backend-authoritative -> 状态以后台真实记录为准
```

## Acceptance

1. a non-programmer can understand the main `/agent` page without knowing English technical terminology;
2. every actionable button and destructive confirmation is Chinese-first;
3. important runtime states are translated consistently across list/detail/timeline pages;
4. backend enum/tool identifiers remain available for audit but never replace the human-readable Chinese explanation;
5. no user-facing English-only error is emitted for normal workbench operations;
6. localization changes do not alter Job/AgentRun/HumanAction authority semantics.

## UI layout review in the same pass

Use this stage to review whether the current vertically stacked engineering-console layout should evolve toward a more operator-oriented AI workbench (for example: task/project navigation, central Agent execution/timeline, evidence/artifact side context). Do not couple that visual redesign to Stage 5 backend correctness.

---

# Stage 6 — Permission-gated real collection

## Goal

Introduce physical/browser tools only after runtime/HITL/resume behavior is proven.

Tools may include:

```text
account.collect_latest_sample
shop.preflight
shop.collect_evidence_sample
qianfan.collect_rank_scope
```

## Hard rules

- tool availability checks device/profile/session prerequisites;
- real external action requires the configured permission class;
- unsafe interruption -> `needs_human`;
- no automatic physical replay after restart;
- every collected result goes through existing service/evidence persistence;
- candidate order/sample semantics do not change;
- pause/cancel/human takeover remain;
- missing Bailian/local-model configuration is not a Stage 6 blocker; when deterministic evidence is ambiguous, route reasoning through the durable ChatGPT handoff/result path instead of treating provider absence as failure.

## Acceptance

Run controlled real UAT for one permitted path and verify DB/files/artifacts/SHA after every stage.

---

# Stage 7 — Business journey surface

## Goal

Move from a collection of pages/jobs into the complete A/B/C/D business journey without forcing a generic Project-first abstraction into the operator UI.

The authoritative product standard is `28_TUTORIAL_ALIGNED_BUSINESS_WORKBENCH_STANDARD.md`.

### 7.1 / 7.2 — Demand Radar business projection: FORMALLY ACCEPTED

The first slice reuses the existing durable chain:

```text
Analysis -> OpportunityRecord -> ProductRecord -> ContentItem
```

and adds a read-only business projection plus tutorial-aligned Demand Radar.

No Project table is required for this slice. Add a lightweight Project/BusinessCase entity later only if it solves a real cross-stage relational-continuity problem that existing stable references cannot solve.

The UI must keep these concepts separate:

```text
implemented
proven
authorized
```

The implementation contract and local validation are recorded in `29_DEMAND_RADAR_BUSINESS_SURFACE_V1.md`.

### 7.2.1 — Demand Radar safe product media: FORMALLY ACCEPTED

Close the remaining A-stage image-inspection gap before Product Definition:

```text
Opportunity supporting product
  -> immutable Analysis snapshot
  -> trusted shop result
  -> exact raw_evidence.detail_screen
  -> same-Job android_screenshot Artifact
  -> transition/SHA/file/image validation
  -> safe browser thumbnail + click-to-inspect
```

Hard boundary:

- no generic Artifact download endpoint;
- no browser-visible local Artifact path;
- no arbitrary Android operation screenshots presented as product evidence;
- only the exact `detail_screen` binding already used by the trusted sample evidence contract is previewable;
- historical rows with image counts but no provable safe binding remain placeholders;
- no Android/XHS replay is allowed merely to render an existing image;
- this slice does not enter Phase B.

Implementation and real historical-data validation are recorded in `30_DEMAND_RADAR_SAFE_MEDIA_V1.md`.

### Important

Do not retroactively rewrite historical Phase A records merely to attach them to Project/BusinessCase. If such an entity is introduced later, use stable references/migration records.

Do not enter Product Definition / Phase B until Stage 7.1/7.2 are formally accepted.

---

# Stage 8 — Product Definition (Phase B) systemization

## Precondition

Run at least the existing real `七宗罪` opportunity through product research manually/AI-assisted enough to verify what information genuinely matters. Do not over-generalize from the tutorial alone.

## Build

Create Product Definition lifecycle:

```text
draft
researching
pending_review
approved | rejected
```

with the required semantic fields/evidence refs from ADR-005.

Agent may:

- retrieve Opportunity evidence;
- research approved external/competitor material;
- synthesize candidate definitions;
- compare alternatives;
- request human review.

Agent may not approve its own definition.

## Exit gate

One real Product Definition is approved through the system and can be independently re-read after restart.

---

# Stage 9 — Product Build handoff (Phase C)

## Goal

Do not turn the XHS research Agent into an unrestricted software builder.

Product Definition produces a typed build brief.

Depending on product type:

```text
virtual document -> dedicated document-production workflow
website/app/software -> isolated Codex/coding project/workspace
other -> type-specific builder
```

Workbench tracks status/evidence/deliverables but build tools stay separately permissioned.

Finished Product requires human UAT and a dossier/manifest before D.

---

# Stage 10 — Content system integration (Phase D)

## Entry gate

`Finished Product + human UAT` only.

Then reuse/revalidate the existing content/media infrastructure and original content-system reference chain:

```text
Finished Product Dossier
 -> product analysis
 -> keywords
 -> benchmark collection
 -> single-note breakdown
 -> template clustering
 -> approved Skill
 -> daily generation
 -> independent review
 -> pending-publication package
```

Do not auto-publish.

---

# Stage 11 — Decide whether a new repository is still necessary

Do **not** create `xhs-workbench-next` merely because that was the original idea.

After Stages 1–4, evaluate:

```text
How much existing code remains healthy?
How much of the new runtime is isolated?
Is frontend migration incremental?
Are current migrations/tests manageable?
Would a new repo reduce risk, or only duplicate history?
```

### Likely outcome based on current audit

Evolving `xhs-ai-workflow` is currently favored because the domain backend is an asset.

A new repository becomes justified only if the spike proves integration boundaries are badly constrained by current structure.

---

# Branch strategy

## Research

Current:

```text
research/xhs-workbench-next
```

Contains knowledge/decisions only.

## First implementation spike

Recommended when implementation starts:

```text
spike/agent-runtime-pydantic-v1
```

created from a known baseline that includes or references these research decisions.

Do not develop the spike directly on `feature/system-v1`.

## Later feature branch

Only after spike acceptance:

```text
feature/agent-runtime-v1
```

or another explicit production-integration branch.

---

# Definition of success for the whole redesign

The redesign succeeds when the user can open one workbench and see a real project move through A/B/C/D while:

- AI chooses and executes bounded work autonomously where safe;
- humans make the intended business decisions;
- every fact/claim is traceable;
- crashes/restarts do not create fake success;
- unsafe work stops for a person;
- Agent context is controlled and measurable;
- model/tool usage is visible;
- existing proven evidence rules are preserved;
- the workbench is pleasant enough to operate without reading logs or knowing database IDs.

The objective is not maximum autonomy. It is **maximum useful autonomy inside a truthful, inspectable and recoverable business system**.
