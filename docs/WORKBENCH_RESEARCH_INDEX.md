# XHS Workbench Next — Research Index

## Purpose

This branch is the isolated research workspace for evaluating a next-generation Xiaohongshu AI workbench.

The goal is to build durable project knowledge before architecture selection or implementation. Research in this branch must not change the current Phase A business behavior unless a later explicit implementation decision says so.

## Authority and guardrails

The existing project remains the source of truth for proven business behavior and acceptance rules. Research must preserve evidence integrity, fail-closed behavior, explicit human gates, and current Phase boundaries.

Recovered or reconstructed proprietary source code may be studied for architecture, but must not be copied or vendored into the product without a valid license and explicit legal basis.

## Research order

1. Understand the tutorial/business workflow.
2. Deeply map the current XHS system: code, data, jobs, evidence, adapters, UI, tests, and real UAT history.
3. Separate proven capabilities from incomplete/experimental capabilities and historical debt.
4. Study mature agent architectures and Claude Code concepts at the design level.
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

- `00_PROJECT_MAP.md`
- `01_TUTORIAL_BUSINESS_MODEL.md`
- `02_CURRENT_SYSTEM_ARCHITECTURE.md`
- `03_CURRENT_SYSTEM_CAPABILITIES.md`
- `04_CURRENT_SYSTEM_PROBLEMS.md`
- `05_CLAUDE_CODE_ARCHITECTURE_NOTES.md`
- `06_AGENT_RUNTIME_REQUIREMENTS.md`
- `07_WORKBENCH_REQUIREMENTS.md`
- `08_BACKEND_REQUIREMENTS.md`
- `09_MIGRATION_RULES.md`
- `10_OPEN_SOURCE_CANDIDATES.md`
- `11_ARCHITECTURE_DECISIONS.md`
- `12_IMPLEMENTATION_ROADMAP.md`

## Rule for future AI sessions

Do not rely on chat memory as the project record. Any material finding, constraint, rejected option, or architecture decision that would otherwise need to be rediscovered must be written into this branch and committed.
