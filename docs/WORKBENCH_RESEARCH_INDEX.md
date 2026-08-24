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

ADRs:

- `docs/adr/ADR-001-preserve-domain-core.md` — accepted
- `docs/adr/ADR-002-agent-runtime-spike.md` — proposed/approved for isolated spike
- `docs/adr/ADR-003-agent-ui-contract.md` — proposed/approved for isolated spike
- `docs/adr/ADR-004-local-durability-first.md` — accepted
- `docs/adr/ADR-005-product-definition-gate.md` — accepted

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
Adapter Registry
        ↓
Qianfan / XHS / Android / Bailian / optional MCP
        ↓
SQLite + managed evidence/artifacts
```

The model gets more autonomy over **sequence**, not over **truth or authority**.

## Next step — implementation spike

Research is now complete enough to stop broad architecture searching.

Next execution should follow `12_IMPLEMENTATION_ROADMAP.md`:

### Stage 0 — baseline freeze

- create an isolated spike branch;
- run current backend/frontend verification;
- record current green/red baseline;
- freeze representative parity/context benchmarks.

### Stage 1 — pure Agent Runtime spike

Recommended branch name:

```text
spike/agent-runtime-pydantic-v1
```

Build only:

- AgentRun/AgentStep persistence;
- Tool protocol/registry;
- PermissionPolicy;
- ContextBuilder skeleton;
- bounded Pydantic AI loop with fake model/fake tools;
- checkpoint/resume;
- lifecycle events/usage tests.

Do not connect Android, Qianfan, live XHS collection, Opportunity approval, Product Build or publishing in the first slice.

### Stage 2+ after spike passes

- wrap one safe read-only domain path;
- run context-token benchmark;
- build AG-UI/CopilotKit UI spike;
- only then introduce real collection and later B/C/D stages.

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
- `11_ARCHITECTURE_DECISIONS.md` — **Created**
- `12_IMPLEMENTATION_ROADMAP.md` — **Created**

### Current implementation knowledge

- `24_REAL_AGENT_ORCHESTRATION_V1.md` — real evidence-grounded Agent launch/continuation boundary.
- `25_AGENT_WORKBENCH_ZH_LAYOUT_V1.md` — Chinese-first three-column operator workbench and UI-component integration boundary.
- `26_CHATGPT_PRIMARY_REASONING_PROVIDER.md` — authoritative provider override: ChatGPT is the primary external reasoning provider; Bailian/local models are optional accelerators, not required workflow dependencies.
- `27_PHYSICAL_COLLECTION_BOUNDARY_V1.md` — first permission-gated real Android `shop.preflight` boundary, ChatGPT-primary initial/follow-up reasoning, physical-result reconciliation, restart safety and controlled real-device UAT.

## Rule for future AI sessions

Do not rely on chat memory as the project record. Any material finding, constraint, rejected option, architecture decision or experiment result that would otherwise need to be rediscovered must be written into this branch and committed.

A new session should read this index, `11_ARCHITECTURE_DECISIONS.md`, `12_IMPLEMENTATION_ROADMAP.md` and the relevant requirement/ADR files before repeating research.

Broad architecture research should not be restarted unless an implementation spike disproves a recorded assumption.
