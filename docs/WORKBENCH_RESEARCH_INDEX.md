# XHS Workbench Next — Research Index

## Purpose

This branch is the isolated research workspace for evaluating a next-generation Xiaohongshu AI workbench.

The goal is to build durable project knowledge before implementation. Research in this branch must not change the current proven business behavior unless a later explicit implementation branch does so under the recorded guardrails.

## Current research status

### Phase 1 — Internal project/tutorial audit: COMPLETE

Completed 2026-08-23.

Durable outputs:

- `docs/architecture-knowledge/00_PROJECT_MAP.md`
- `01_TUTORIAL_BUSINESS_MODEL.md`
- `02_CURRENT_SYSTEM_ARCHITECTURE.md`
- `03_CURRENT_SYSTEM_CAPABILITIES.md`
- `04_CURRENT_SYSTEM_PROBLEMS.md`
- `09_MIGRATION_RULES.md`

Conclusion: the current system is a hardened evidence/state modular monolith rather than a generic Agent Runtime. Jobs, evidence, adapters, validators, business gates and real UAT history are migration assets.

### Phase 2 — External agent architecture study + requirements: COMPLETE

Completed 2026-08-23.

Studied:

- reconstructed Claude Code 2.1.88 architecture — study only; no open-source license found for the reconstructed proprietary source;
- Pydantic AI + Pydantic AI Harness — MIT;
- OpenHands Agent Canvas + Software Agent SDK — MIT;
- LangGraph/checkpoint architecture — MIT;
- Microsoft Agent Framework — MIT;
- AutoGen — historical reference; current upstream is in maintenance mode.

Source research:

- `docs/source-research/CLAUDE_CODE_2_1_88_ARCHITECTURE_STUDY.md`
- `docs/source-research/PYDANTIC_AI_HARNESS_STUDY.md`
- `docs/source-research/OPENHANDS_AGENT_CANVAS_STUDY.md`
- `docs/source-research/DURABLE_ORCHESTRATION_PATTERNS_STUDY.md`

Derived requirements:

- `05_CLAUDE_CODE_ARCHITECTURE_NOTES.md`
- `06_AGENT_RUNTIME_REQUIREMENTS.md`
- `07_WORKBENCH_REQUIREMENTS.md`
- `08_BACKEND_REQUIREMENTS.md`

### Phase 3 — Open-source candidate selection: COMPLETE (research recommendation)

Completed 2026-08-23.

Detailed scorecard:

- `docs/architecture-knowledge/10_OPEN_SOURCE_CANDIDATES.md`

Additional candidates evaluated include:

- AG-UI — MIT;
- CopilotKit — MIT;
- assistant-ui — MIT;
- AionUi — Apache-2.0;
- Langflow — MIT;
- Refly — rejected because its Apache-derived license adds commercial/frontend restrictions.

#### Recommended stack for isolated spike

```text
Runtime:
  Pydantic AI + selected Pydantic AI Harness capabilities

Agent/UI interaction:
  AG-UI-compatible event contract

React Agent/HITL primitives:
  selective CopilotKit
  assistant-ui as backup primitive library

Workbench shell:
  evolve our current XHS React shell/domain pages
  OpenHands Agent Canvas + AionUi are UX/component references, not monolithic foundations

Backend:
  retain current FastAPI + SQLite + JobService + domain services

Durability:
  add AgentRun / AgentStep / PermissionDecision / HumanAction / Checkpoint in SQLite
  do not add a distributed workflow engine in V1
```

The AG-UI repository contains verified Pydantic AI integration/examples, so this is a real compatibility path rather than an invented three-project combination.

### Phase 4 — Architecture decisions + implementation roadmap: COMPLETE

Completed 2026-08-23.

Core outputs:

- `docs/architecture-knowledge/11_ARCHITECTURE_DECISIONS.md`
- `docs/architecture-knowledge/12_IMPLEMENTATION_ROADMAP.md`
- `docs/architecture-knowledge/13_RUNTIME_EXPERIMENT_RESULTS.md`
- `docs/architecture-knowledge/14_IMPLEMENTATION_SCOPE_OVERRIDE.md`

ADRs:

- `docs/adr/ADR-001-preserve-domain-core.md` — accepted
- `docs/adr/ADR-002-agent-runtime-spike.md` — proposed/approved for isolated spike
- `docs/adr/ADR-003-agent-ui-contract.md` — proposed/approved for isolated spike
- `docs/adr/ADR-004-local-durability-first.md` — accepted
- `docs/adr/ADR-005-product-definition-gate.md` — accepted
- `docs/adr/ADR-006-defer-bc-systemization.md` — accepted

## Current B/C scope decision

The A/B/C/D labels remain useful business concepts, but B and C are **not current workflow-automation targets**.

```text
A system
  Opportunity
      ↓
  human approval
      ↓
B — external / human + AI / product-specific
  output: Product Definition handoff
      ↓
C — external / human + AI / Codex / product-specific
  output: Finished Product + human UAT handoff
      ↓
D system
  Content research / production
```

Do not build a generic B Agent, C Agent, B/C state machine, universal product builder or speculative B/C workflow until enough varied completed products exist to demonstrate recurring patterns.

When older roadmap Stage 7–10 text conflicts with this decision, `ADR-006` and `14_IMPLEMENTATION_SCOPE_OVERRIDE.md` are authoritative.

## Central architecture decision

Do **not** merge Claude Code source + a giant workbench + a second generic backend.

Target:

```text
XHS Workbench UI
        ↓
Stable API + AG-UI-compatible event layer
        ↓
Bounded Agent Runtime
  ContextBuilder / ToolRegistry / PermissionPolicy
  Budgets / Checkpoints / Events
        ↓
Existing hardened Domain Services
        ↓
Adapter Registry / durable Jobs
        ↓
Qianfan / XHS / Android / Bailian / optional MCP
        ↓
SQLite + managed evidence/artifacts
```

The model gets more autonomy over **sequence**, not over **truth or authority**.

## Implementation status

### Stage 0 / Stage 1 — baseline + Agent Runtime: IMPLEMENTED IN SPIKE

Primary spike branch:

```text
spike/agent-runtime-pydantic-v1
```

The isolated runtime has durable runs/steps, permissions, human waits, checkpoints, budgets, typed tools, fail-closed uncertain-side-effect handling, recovery semantics and regression comparison infrastructure.

### Stage 2 — safe real domain path: PROVEN IN SPIKE

The existing `AnalysisService` was wrapped rather than rewritten. Controlled parity tests preserve business status/evidence behavior.

### Stage 3 — context compaction: PROVEN AS ISOLATED EXPERIMENT

Branch/PR experiment proves deterministic model projection can materially reduce prompt structure without deleting authoritative raw evidence or changing service grounding. Real provider token savings still require a controlled live provider A/B before claiming an exact token number.

### Runtime cleanup still pending dynamic acceptance

Draft PR #6 (`fix/agent-runtime-foldin-v1`) makes the hardened Stage 2 semantics the canonical `runtime.AgentRuntime` entry and removes the easy bypass through two public implementations.

Its latest dynamic verification is blocked by GitHub Actions jobs failing before any step is created. Do not claim PR #6 passed until the jobs actually execute and pass.

## Next implementation direction

After canonical Runtime dynamic acceptance:

1. define the minimal relationship between `AgentRun` and the existing durable `Job` lifecycle;
2. keep `JobService` authoritative for lease/task/physical-worker lifecycle;
3. do not let synchronous Agent tool execution replace physical XHS/Android workers;
4. expose safe Job-oriented Agent tools such as create/read/status/result rather than direct raw device actions;
5. continue toward Project/Run/Human Action/Evidence workbench views;
6. preserve only thin B/C handoffs;
7. connect D after Finished Product + human UAT.

## Authority and guardrails

The existing project remains the source of truth for proven business behavior and acceptance rules. Research/implementation must preserve evidence integrity, fail-closed behavior, explicit human gates and current phase boundaries.

Recovered/reconstructed proprietary source may be studied for architecture but must not be copied or vendored without valid license/legal basis.

## Core document status

- `00_PROJECT_MAP.md` — **Created**
- `01_TUTORIAL_BUSINESS_MODEL.md` — **Created**
- `02_CURRENT_SYSTEM_ARCHITECTURE.md` — **Created**
- `03_CURRENT_SYSTEM_CAPABILITIES.md` — **Created**
- `04_CURRENT_SYSTEM_PROBLEMS.md` — **Created**
- `05_CLAUDE_CODE_ARCHITECTURE_NOTES.md` — **Created**
- `06_AGENT_RUNTIME_REQUIREMENTS.md` — **Created**
- `07_WORKBENCH_REQUIREMENTS.md` — **Created**
- `08_BACKEND_REQUIREMENTS.md` — **Created**
- `09_MIGRATION_RULES.md` — **Created**
- `10_OPEN_SOURCE_CANDIDATES.md` — **Created**
- `11_ARCHITECTURE_DECISIONS.md` — **Updated for ADR-006**
- `12_IMPLEMENTATION_ROADMAP.md` — **Historical roadmap; Stage 7–10 partially superseded**
- `13_RUNTIME_EXPERIMENT_RESULTS.md` — **Created**
- `14_IMPLEMENTATION_SCOPE_OVERRIDE.md` — **Active override**

## Rule for future AI sessions

Do not rely on chat memory as the project record. Any material finding, constraint, rejected option, architecture decision or experiment result that would otherwise need to be rediscovered must be written into this branch and committed.

A new session should read this index, `11_ARCHITECTURE_DECISIONS.md`, `14_IMPLEMENTATION_SCOPE_OVERRIDE.md`, the relevant requirement/ADR files, and then the historical roadmap before repeating research.

Broad architecture research should not be restarted unless an implementation spike disproves a recorded assumption.