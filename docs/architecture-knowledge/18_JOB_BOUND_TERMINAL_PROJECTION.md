# 18 — Job-bound Terminal Projection Contract

Status: architecture decision / implementation constraint

## Purpose

Close the final lifecycle gap between a durable terminal AgentRun and the authoritative durable Job without letting stale Agent state overwrite cancellation, continuation, lease recovery, or missing evidence.

## Authority model

The Job remains authoritative. AgentRun is the execution trace.

A terminal AgentRun may project into the Job only when that run is still the single unambiguous latest binding for an `agent_orchestration` Job and the same Job claim generation is still being finalized.

The final write must be a compare-and-swap over at least:

- Job id/type;
- Job state = `running`;
- observed `retry_count`;
- observed `lease_expires_at`;
- run is still the unique latest AgentJob binding at the SQL write boundary.

A point-in-time guard is not sufficient.

## Durable proof before terminal Job state

### Success

Before `Job.running -> succeeded`, require:

- AgentRun state is durable `succeeded`;
- AgentRun has durable `completed_at`;
- `final_output_json` exists;
- a durable succeeded output step exists and exactly matches the final output;
- no pending HumanAction remains for the run;
- no Tool step remains `started`, `uncertain`, or `needs_human`;
- any durable completion policy on the Job is satisfied.

Current completion-policy keys are staged under `job.input_data.agent_completion`:

- `require_evidence_refs: bool`;
- `required_artifact_kinds: list[str]`.

Evidence requirements are explicit rather than globally forced because some valid deterministic/synthesis tasks may have no external evidence, while evidence-backed research Jobs can require it.

When evidence is required, it is evaluated over the **entire durable Job-bound AgentRun history**, not only the final continuation run. A prior run may have collected valid evidence before entering `needs_human`, while a later continuation run only performs the approved action and finishes. Historical evidence is read through the durable Job bindings; it is not copied into the child AgentRun. A missing historical bound AgentRun makes the evidence history incomplete and must fail closed.

### Failure

Before `Job.running -> failed`, require:

- AgentRun state is durable `failed`;
- AgentRun has durable `completed_at`;
- error category/detail exist;
- a durable failed error step exists and matches the AgentRun terminal error;
- no pending HumanAction remains;
- no unresolved Tool step remains.

## Lease / crash rule

Normal projection happens immediately after the AgentRun terminal proof is committed.

For restart recovery, current wall-clock time may already be past the old Job lease. That alone does not invalidate a terminal result if durable timestamps prove:

`agent_run.completed_at <= observed_job.lease_expires_at`

This proves the AgentRun reached terminal state while that claim still owned authority. The reconciler may then perform the same terminal Job CAS without replaying any model or Tool work.

If the AgentRun terminal timestamp is after the observed lease expiry, it may not finalize the Job. The trace is retained and ordinary Job lease recovery remains authoritative.

## Race rules

- If Job is already `cancelled`, cancellation wins.
- If Job is already `succeeded`/`failed`, terminal Job state is never reopened.
- If a newer continuation binding exists, the older terminal AgentRun is stale and cannot finalize the Job.
- If claim generation changes between proof read and write, the CAS must fail closed.
- A terminal projector never replays a model call or Tool handler.
- Missing output/error/evidence proof is not repaired by trusting model text; it is a projection failure that requires recovery/inspection.

## Integration strategy

First validate this behavior in an isolated terminal lifecycle adapter stacked on the proven Job-bound runtime. After dedicated and broad dynamic gates are green, fold the behavior into the canonical Job-bound composition without duplicating the execution loop.

## Non-goals

- No Workbench UI/API in this slice.
- No Android/XHS/browser physical-worker ownership.
- No generic B/C product workflow.
- No automatic acceptance of ChatGPT handoff results; those follow the separate AgentDock handoff contract.
