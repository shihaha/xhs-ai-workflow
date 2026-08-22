# ADR-004: Keep local SQLite/Job durability for the first Agent Runtime

- Status: accepted
- Date: 2026-08-23

## Context

LangGraph, Temporal-style systems and Microsoft Agent Framework provide strong durable workflow/checkpoint primitives. However, the current product is V1 single-machine/single-user and already has a hardened SQLite JobService with leases, CAS transitions, explicit interruption recovery, artifacts and real UAT.

Adding a distributed workflow engine now would not solve the main missing problem: bounded Agent orchestration over existing business services.

## Decision

For the first next-gen runtime:

- retain JobService as the outer durable business-work envelope;
- persist AgentRun/AgentStep/PermissionDecision/HumanAction/Checkpoint in SQLite;
- checkpoint only after authoritative domain persistence is proven;
- use explicit idempotency and safe resume rules;
- preserve `needs_human` for uncertain/unsafe physical interruption;
- do not add LangGraph/Temporal/DBOS/Prefect/Restate/Microsoft durable hosting yet.

Reevaluate only if measured requirements exceed local durability.

## Alternatives considered

- LangGraph checkpointer — strong, but adds graph runtime before it is needed.
- Temporal or similar — excellent distributed durability, excessive operational complexity for current V1.
- Microsoft Agent Framework durable workflow — broad production option, reserved for future scale.

## Consequences

Positive:

- minimal new infrastructure;
- preserves proven restart/failure behavior;
- easiest path to isolated runtime experiment;
- lower setup/maintenance cost.

Limitations:

- we own Agent checkpoint/event persistence;
- not designed for multi-machine high-throughput orchestration;
- future graph/distributed requirements may justify migration.

## Re-evaluation triggers

- multiple machines/users;
- complex parallel workflow graphs;
- long distributed waits;
- high task concurrency/throughput;
- local Job/runtime implementation becoming a measured maintenance bottleneck.

## Evidence / references

- `backend/app/services/jobs.py`
- `docs/source-research/DURABLE_ORCHESTRATION_PATTERNS_STUDY.md`
- `docs/architecture-knowledge/06_AGENT_RUNTIME_REQUIREMENTS.md`
- `docs/architecture-knowledge/08_BACKEND_REQUIREMENTS.md`