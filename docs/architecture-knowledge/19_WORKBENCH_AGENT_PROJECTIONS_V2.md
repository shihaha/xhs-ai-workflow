# 19 — Workbench Agent Durable Projections V2

Status: implementation slice

## Purpose

Extend the first read-only Agent workbench API from single Job/Run detail into safe collection views that a later React workbench can consume without inventing authority or reading transient process memory.

The source of truth remains:

- Job lifecycle: `JobService` / SQLite;
- Agent execution history: durable AgentRun / AgentStep records;
- Human waits: durable HumanAction records;
- evidence links: persisted AgentStep evidence refs;
- artifacts: durable JobArtifact records.

No projection in this slice may claim, resume, approve, cancel, execute a model, execute a Tool, or control a physical worker.

## Added read surfaces

- `GET /api/v1/agent-runtime/jobs`
  - Agent-orchestration Jobs only;
  - current binding summary;
  - run / pending-human / evidence / artifact counts;
  - no raw Job input.

- `GET /api/v1/agent-runtime/runs`
  - Job-bound AgentRun summaries with authoritative `job_id`;
  - no raw model/tool payloads.

- `GET /api/v1/agent-runtime/human-actions?status=...`
  - durable HumanAction summaries across Agent Jobs;
  - optional `pending | approved | denied` filter;
  - no request/resolution JSON.

- `GET /api/v1/agent-runtime/jobs/{job_id}/artifacts`
  - artifact identity, kind, producer and creation time only;
  - no local path and no arbitrary metadata on the generic surface.

- `GET /api/v1/agent-runtime/jobs/{job_id}/evidence`
  - ordered de-duplicated durable evidence refs aggregated across Job-bound run history.

The existing Job detail projection also gains created/updated timestamps plus the same redacted artifact summaries.

## Redaction rule

Generic workbench collection/detail views deliberately omit:

- Job `input_data`;
- AgentStep `input_json` / `output_json`;
- HumanAction `request_json` / `resolution_json`;
- artifact filesystem paths;
- artifact arbitrary metadata;
- cookies, credentials, browser/device state and provider prompts.

Explicit capabilities may expose a bounded subset later, but that must be a separate contract rather than widening these generic projections.

## Projects boundary

There is currently no authoritative durable `Project` entity in this ancestry. Therefore this slice does **not** synthesize a Project from Job input, folder names or UI state.

A future Projects view must be backed by a real durable project identity/relationship before it becomes part of the workbench API.

## ChatGPT handoff task boundary

The durable AgentDock/manual-ChatGPT handoff implementation is currently isolated in a parallel spike. This projection slice does not copy or partially recreate that table/contract.

Once the handoff durability slice is in the same ancestry, the workbench may add a read-only handoff-task projection keyed by durable `handoff_id`, Job, AgentRun and HumanAction identity. Until then, generic HumanAction projection is the only handoff-adjacent view here.

## B/C and physical-worker boundary

No generic B product-research workflow and no generic C product-build workflow is introduced.

No Android, XHS or browser physical worker is exposed as an Agent Tool or workbench mutation. The required authority path remains:

`Agent -> durable Job / JobService -> physical worker -> Evidence / Artifact / Job state`.

## Acceptance

The dedicated gate must prove:

- collection endpoints reconstruct from SQLite after app restart;
- non-Agent Jobs are excluded/rejected;
- cross-Job HumanAction identity remains bound to the correct Job;
- evidence refs remain ordered/de-duplicated;
- artifact paths/metadata and other raw inputs do not leak;
- the original first-slice read API remains green;
- existing Jobs API tests and complete Agent Runtime acceptance remain green.

The repository-wide Agent Runtime regression workflow remains an additional required gate. No merge is automatic.
