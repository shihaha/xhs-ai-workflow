# XHS Workbench Next — Research Index

## Purpose

This branch is the isolated research workspace for evaluating a next-generation Xiaohongshu AI workbench.

The goal is to build durable project knowledge before architecture selection or implementation. Research in this branch must not change the current proven business behavior unless a later explicit implementation decision says so.

## Current research status

### Phase 1 — Internal project/tutorial audit: COMPLETE (first deep pass)

Completed 2026-08-23. Durable outputs:

- `docs/architecture-knowledge/00_PROJECT_MAP.md`
- `docs/architecture-knowledge/01_TUTORIAL_BUSINESS_MODEL.md`
- `docs/architecture-knowledge/02_CURRENT_SYSTEM_ARCHITECTURE.md`
- `docs/architecture-knowledge/03_CURRENT_SYSTEM_CAPABILITIES.md`
- `docs/architecture-knowledge/04_CURRENT_SYSTEM_PROBLEMS.md`
- `docs/architecture-knowledge/09_MIGRATION_RULES.md`

This pass established the current system as a hardened evidence/state modular monolith rather than a generic Agent Runtime, separated implemented code from currently authorized business stages, and froze migration guardrails.

### Phase 2 — External agent architecture study + requirements: COMPLETE

Completed 2026-08-23.

External systems studied:

- reconstructed Claude Code 2.1.88 architecture — study only; no open-source license found for the reconstructed proprietary source;
- Pydantic AI + Pydantic AI Harness — MIT;
- OpenHands Agent Canvas + Software Agent SDK — MIT;
- LangGraph/checkpoint architecture — MIT;
- Microsoft Agent Framework — MIT;
- AutoGen as historical reference only; current repository is in maintenance mode and points new projects to Microsoft Agent Framework.

Durable source-research outputs:

- `docs/source-research/CLAUDE_CODE_2_1_88_ARCHITECTURE_STUDY.md`
- `docs/source-research/PYDANTIC_AI_HARNESS_STUDY.md`
- `docs/source-research/OPENHANDS_AGENT_CANVAS_STUDY.md`
- `docs/source-research/DURABLE_ORCHESTRATION_PATTERNS_STUDY.md`

Derived XHS architecture requirements:

- `docs/architecture-knowledge/05_CLAUDE_CODE_ARCHITECTURE_NOTES.md`
- `docs/architecture-knowledge/06_AGENT_RUNTIME_REQUIREMENTS.md`
- `docs/architecture-knowledge/07_WORKBENCH_REQUIREMENTS.md`
- `docs/architecture-knowledge/08_BACKEND_REQUIREMENTS.md`

### Phase 2 key conclusion

Do **not** merge Claude Code source + a random workbench + a random backend.

The target architecture is now constrained as:

```text
Workbench UI / control surface
        ↓
Stable Workbench API + event stream
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

The current Jobs/evidence/adapters/validators/business gates are migration assets. The new layer is orchestration, context, permission, run/step persistence and generic workbench primitives.

### Phase 3 — Open-source selection: NEXT

Now that requirements are explicit:

1. search licensed Workbench UI candidates;
2. search licensed Agent Runtime/orchestration candidates;
3. evaluate backend/task/runtime components only where an actual gap remains;
4. score maintenance, licensing, Windows/local fit, integration cost and removal cost;
5. test the most promising candidates against a minimal integration spike where necessary;
6. write `10_OPEN_SOURCE_CANDIDATES.md` plus per-candidate evaluations;
7. record architecture-changing choices as ADRs.

Important candidate families already identified for deeper Phase 3 evaluation, without selecting them yet:

- Pydantic AI / Pydantic AI Harness for typed runtime/capabilities;
- OpenHands Agent Canvas for workbench-shell patterns/components;
- LangGraph or Microsoft Agent Framework only where their durability/workflow primitives solve a measured need rather than adding infrastructure for its own sake.

### Phase 4 — Architecture decision and implementation roadmap

Outputs:

- `11_ARCHITECTURE_DECISIONS.md`
- `12_IMPLEMENTATION_ROADMAP.md`
- ADRs

Only then decide whether to evolve `xhs-ai-workflow` or create a new `xhs-workbench-next` repository.

## Authority and guardrails

The existing project remains the source of truth for proven business behavior and acceptance rules. Research must preserve evidence integrity, fail-closed behavior, explicit human gates, and current Phase boundaries.

Recovered or reconstructed proprietary source code may be studied for architecture, but must not be copied or vendored into the product without a valid license and explicit legal basis.

## Research order

1. Understand the tutorial/business workflow. **DONE — first deep pass.**
2. Deeply map the current XHS system. **DONE — first deep pass.**
3. Separate proven capabilities from incomplete/experimental capabilities and historical debt. **DONE — first deep pass.**
4. Study mature agent architectures and Claude Code concepts. **DONE.**
5. Derive concrete Agent Runtime, workbench and backend requirements. **DONE.**
6. Search and evaluate open-source candidates against those requirements and licenses. **NEXT.**
7. Record architecture decisions as ADRs.
8. Decide whether to evolve this repository or create `xhs-workbench-next`.

## Knowledge map

- `docs/architecture-knowledge/` — durable understanding of our product and target architecture.
- `docs/source-research/` — external architecture/source studies.
- `docs/open-source-evaluation/` — candidate repositories, licenses, fit scores, rejection reasons and spikes.
- `docs/adr/` — Architecture Decision Records.

## Core documents

- `00_PROJECT_MAP.md` — **Created.**
- `01_TUTORIAL_BUSINESS_MODEL.md` — **Created.**
- `02_CURRENT_SYSTEM_ARCHITECTURE.md` — **Created.**
- `03_CURRENT_SYSTEM_CAPABILITIES.md` — **Created.**
- `04_CURRENT_SYSTEM_PROBLEMS.md` — **Created.**
- `05_CLAUDE_CODE_ARCHITECTURE_NOTES.md` — **Created.**
- `06_AGENT_RUNTIME_REQUIREMENTS.md` — **Created.**
- `07_WORKBENCH_REQUIREMENTS.md` — **Created.**
- `08_BACKEND_REQUIREMENTS.md` — **Created.**
- `09_MIGRATION_RULES.md` — **Created.**
- `10_OPEN_SOURCE_CANDIDATES.md` — Phase 3.
- `11_ARCHITECTURE_DECISIONS.md` — Phase 4.
- `12_IMPLEMENTATION_ROADMAP.md` — Phase 4.

## Rule for future AI sessions

Do not rely on chat memory as the project record. Any material finding, constraint, rejected option, architecture decision or experiment result that would otherwise need to be rediscovered must be written into this branch and committed.

When a new research session starts, read this index and the already-created knowledge documents before repeating broad repository scans.