# ADR-001: Preserve the hardened XHS domain core

- Status: accepted
- Date: 2026-08-23

## Context

The current repository has real UAT-backed behavior for Jobs, evidence/artifact integrity, XHS/Android collection, strict analysis grounding, current-evidence revalidation and one-way human Opportunity review. The project's main missing capability is a reusable Agent orchestration layer and richer workbench shell, not a generic CRUD/backend foundation.

Replacing the FastAPI/SQLite/domain-service core with a third-party agent backend would discard the highest-value proven behavior and force business invariants to be rebuilt inside another platform.

## Decision

Keep the current FastAPI + SQLAlchemy + SQLite domain backend as the authoritative V1 business core.

Add the next Agent Runtime **above** domain services. Agent-visible tools will call hardened domain services; they will not bypass them with raw SQL/ADB/browser access.

Existing Job/evidence/business records remain authoritative. New runtime records such as AgentRun/AgentStep/PermissionDecision/HumanAction/Checkpoint are operational layers that reference domain truth rather than replace it.

## Alternatives considered

- OpenHands Agent Server as new backend — rejected as unnecessary coding-agent/workspace duplication.
- Langflow backend — rejected because it solves generic workflow authoring/deployment rather than our evidence-backed business invariants.
- Microsoft Agent Framework as complete backend — deferred; too broad for current single-user/local requirement.
- full rewrite around a new database/workflow engine — rejected because no measured requirement justifies the migration risk.

## Consequences

Positive:

- preserves proven UAT/failure semantics;
- minimizes migration risk;
- lets runtime/framework candidates remain replaceable;
- keeps business truth independent from the model/framework.

Costs:

- some current large service/db modules still need gradual refactoring;
- we must write a small domain Tool facade and runtime persistence ourselves;
- third-party frameworks cannot dictate our state model.

## Evidence / references

- `docs/architecture-knowledge/02_CURRENT_SYSTEM_ARCHITECTURE.md`
- `docs/architecture-knowledge/03_CURRENT_SYSTEM_CAPABILITIES.md`
- `docs/architecture-knowledge/04_CURRENT_SYSTEM_PROBLEMS.md`
- `docs/architecture-knowledge/08_BACKEND_REQUIREMENTS.md`
- `docs/architecture-knowledge/10_OPEN_SOURCE_CANDIDATES.md`
- current Job/Analysis/Evidence code and Phase A UAT history