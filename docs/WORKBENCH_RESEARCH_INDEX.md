# XHS Workbench Next — Research Index

## Purpose

This branch is the isolated research workspace for evaluating a next-generation Xiaohongshu AI workbench.

The goal is to build durable project knowledge before architecture selection or implementation. Research in this branch must not change the current Phase A business behavior unless a later explicit implementation decision says so.

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

### Phase 2 — External agent architecture study: NEXT

Deeply study Claude Code architectural concepts and other mature agent runtimes at the design level. The goal is to derive requirements, not to copy proprietary recovered source.

Expected outputs:

- `05_CLAUDE_CODE_ARCHITECTURE_NOTES.md`
- `06_AGENT_RUNTIME_REQUIREMENTS.md`
- `07_WORKBENCH_REQUIREMENTS.md`
- `08_BACKEND_REQUIREMENTS.md`

### Phase 3 — Open-source selection: AFTER REQUIREMENTS

Only after Phase 2 requirements are explicit:

- search licensed Workbench UI candidates;
- search licensed Agent Runtime/orchestration candidates;
- evaluate backend/task/runtime components only where an actual gap remains;
- score maintenance, licensing, Windows/local fit, integration cost and removal cost;
- write `10_OPEN_SOURCE_CANDIDATES.md` and per-candidate evaluations;
- record architecture-changing decisions as ADRs.

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
2. Deeply map the current XHS system: code, data, jobs, evidence, adapters, UI, tests, and real UAT history. **DONE — first deep pass.**
3. Separate proven capabilities from incomplete/experimental capabilities and historical debt. **DONE — first deep pass.**
4. Study mature agent architectures and Claude Code concepts at the design level. **NEXT.**
5. Derive concrete Agent Runtime, workbench, backend, context, permission, and persistence requirements.
6. Search and evaluate open-source candidates against those requirements and licenses.
7. Record architecture decisions as ADRs.
8. Only then decide whether to evolve the current repository or create `xhs-workbench-next`.

## Knowledge map

- `docs/architecture-knowledge/` — durable understanding of our product and target architecture.
- `docs/source-research/` — notes from external architecture/source studies.
- `docs/open-source-evaluation/` — candidate repositories, licenses, fit scores, and rejection reasons.
- `docs/adr/` — Architecture Decision Records.

## Planned core documents

- `00_PROJECT_MAP.md` — current system/business map. **Created.**
- `01_TUTORIAL_BUSINESS_MODEL.md` — tutorial and shared-package business model. **Created.**
- `02_CURRENT_SYSTEM_ARCHITECTURE.md` — code/runtime architecture. **Created.**
- `03_CURRENT_SYSTEM_CAPABILITIES.md` — implemented vs real-UAT vs latent capability inventory. **Created.**
- `04_CURRENT_SYSTEM_PROBLEMS.md` — prioritized gaps/debt. **Created.**
- `05_CLAUDE_CODE_ARCHITECTURE_NOTES.md` — next external study.
- `06_AGENT_RUNTIME_REQUIREMENTS.md`
- `07_WORKBENCH_REQUIREMENTS.md`
- `08_BACKEND_REQUIREMENTS.md`
- `09_MIGRATION_RULES.md` — migration guardrails. **Created.**
- `10_OPEN_SOURCE_CANDIDATES.md`
- `11_ARCHITECTURE_DECISIONS.md`
- `12_IMPLEMENTATION_ROADMAP.md`

## Rule for future AI sessions

Do not rely on chat memory as the project record. Any material finding, constraint, rejected option, or architecture decision that would otherwise need to be rediscovered must be written into this branch and committed.

When a new research session starts, read this index and the already-created knowledge documents before repeating broad repository scans.
