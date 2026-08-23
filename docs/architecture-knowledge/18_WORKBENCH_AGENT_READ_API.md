# Workbench Agent Read API — first slice

## Purpose

Expose the already-proven Job-bound Agent lifecycle to the local workbench UI without widening authority or leaking raw execution inputs.

This slice is read-only. JobService remains lifecycle authority; Agent Runtime remains execution trace authority.

## Endpoints

- `GET /api/v1/agent-runtime/jobs/{job_id}`
  - authoritative Job lifecycle summary;
  - ordered bound AgentRun summaries across continuations;
  - current/latest run id when unambiguous;
  - pending HumanAction summaries;
  - de-duplicated durable evidence refs aggregated across Job-bound runs.

- `GET /api/v1/agent-runtime/runs/{run_id}`
  - run summary;
  - ordered step timeline;
  - durable evidence refs;
  - error/output proof summary.

## Deliberate redactions

The generic workbench read surface must not expose by default:

- raw Job input;
- raw Tool arguments;
- provider prompt/context payloads;
- HumanAction raw request payloads;
- cookies, credentials, browser/device state;
- unrestricted artifact file contents.

The future AgentDock ChatGPT handoff endpoint is a separate, explicit capability. It will generate a bounded task package for one pending handoff and is not implemented by this read-only slice.

## Lifecycle rule

The workbench UI observes durable state; it does not infer lifecycle from transient process memory. Restarting the local FastAPI process must reconstruct the same Job/Run/HumanAction/Evidence projection from SQLite.

## Scope boundary

No B/C generic workflow. No Android/XHS/browser physical-worker control. No model execution endpoint. No approval mutation endpoint in this slice.
