# Job-bound Terminal Finalization Contract

Status: research / implementation contract

This document defines the terminal boundary after the approved-continuation slice. It does not merge any spike, does not change the B/C scope override, and does not authorize Agent control of Android/XHS/browser workers.

## Core rule

`AgentRun.succeeded` is execution-trace truth. `Job.succeeded` is authoritative workflow truth. A model returning `finish` is therefore necessary but not sufficient to finalize the Job.

The Job may reach a terminal state only through a write-boundary CAS that proves the finishing run still owns the current Job claim and that the required result/evidence gate is satisfied from durable data.

## Success preconditions

A success projector must verify all of the following from durable state:

1. the run has a durable Job binding;
2. the bound Job type is `agent_orchestration`;
3. the run is the single unambiguous latest binding for that Job;
4. the AgentRun is durably `succeeded`;
5. the Job is still `running` under the same observed claim generation;
6. the Job lease is present and not expired at the finalization boundary;
7. `AgentRun.final_output_json` is present;
8. a durable succeeded output step exists and its payload matches `final_output_json`;
9. any evidence/result policy required by the system is satisfied from durable successful Tool steps / domain records, not from model assertions.

Then, and only then, the final SQL UPDATE may CAS `Job.running -> Job.succeeded`, clear the lease, set `completed_at`, and persist a narrow terminal audit record/log.

## Evidence policy must be system-owned

Do not let the model decide whether evidence is required.

Current grounded domain tools such as `analysis.run_grounded` already return durable `evidence_refs`. The terminal projector may aggregate evidence references only from Tool steps whose durable status is `succeeded`/`reused` and whose persisted result carries those refs.

The first production policy should be explicit and system-owned, for example an injected `TerminalSuccessPolicy(require_evidence=True)` for evidence-backed orchestration. Tests may also prove a no-evidence policy for deliberately non-grounded orchestration, but the default evidence-backed path must fail closed if its required evidence is absent.

Do not treat arbitrary `final_output_json["evidence_refs"]` supplied by a model as proof. Model output may reference durable evidence IDs, but the projector must verify those IDs against durable Tool/domain records before success.

## Write-boundary TOCTOU protection

A pre-check is not a capability. Between validation and the terminal write, an operator may cancel the Job, lease recovery may move it to `needs_human`, or a newer continuation claim may appear.

Therefore the success UPDATE itself must prove:

- Job id/type/state;
- the observed claim generation (`retry_count` plus lease identity/value);
- lease still valid at the write timestamp;
- finishing run remains the unique latest binding at that same SQL boundary.

If the CAS loses, fail closed and honor current Job truth. Never retry the terminal write blindly.

## Terminal race outcomes

### Operator cancellation wins

If Job becomes `cancelled` before the success CAS, the AgentRun may remain durably succeeded as historical trace, but the Job remains cancelled. The Agent must not reopen it.

### Lease recovery wins

If the lease is expired or Job has already moved to `needs_human`, a stale Agent success cannot finalize the Job. Recovery/human authority wins.

### New continuation wins

If a newer binding exists, the historical run cannot finalize the Job even if that historical run later records success.

### Duplicate finalization

A repeated projector call after the same run has already finalized the Job may return an idempotent “already finalized by this run” result only if a durable terminal audit record identifies that exact run. Otherwise terminal state is treated as authoritative and no write is attempted.

## Failure projection is a separate policy problem

Do **not** map every `AgentRun.failed` to `Job.failed` generically.

Examples:

- `job_authority_lost`: must not mutate Job at all; the Agent explicitly lacks authority.
- uncertain external side effect: current Runtime uses `needs_human`, not failed, and Job wait projection owns that path.
- operator cancellation: Job cancellation is authoritative; Agent failure/cancellation trace must not overwrite it.
- budget exhaustion may eventually deserve a recoverable human policy rather than irreversible Job failure.
- deterministic model/tool validation/execution failures may be eligible for Job failure, but only under an explicit system-owned category policy and the same latest-run/claim CAS rules.

For safety, the next implementation slice should project **success only**. Failure-category mapping should be a following isolated slice after its recovery semantics are decided and tested.

## Result audit record

A thin separate table is preferable to stuffing model output into Job input/log text. Suggested spike shape:

```text
agent_job_terminal_results
  job_id PK
  run_id UNIQUE
  outcome            # succeeded for first slice
  final_output_json
  evidence_refs_json
  created_at
```

No cross-metadata FK is required in the spike; application-level validation must enforce identity, consistent with existing Agent/Job metadata isolation.

The terminal record and Job state transition should commit in the same SQLite transaction.

## No physical-worker takeover

A terminal Agent Job may summarize/coordinate durable domain Jobs and consume their evidence. It must not mark an Android/XHS physical Job succeeded merely because the model says work is done. Physical Job terminal state remains owned by the existing domain worker/JobService path.

## First success-slice acceptance

At minimum prove:

- direct model `finish` with missing required evidence cannot finalize an evidence-required Job;
- durable final output + matching output step + valid durable evidence can finalize;
- model-declared fake evidence IDs are rejected;
- cancelled Job wins against late Agent success;
- expired lease blocks finalization;
- old/superseded run cannot finalize after a continuation exists;
- race creating a newer binding after pre-check but before UPDATE loses the terminal CAS;
- insert conflict on terminal audit record rolls back Job success;
- repeated finalization by the same run is idempotent only with matching durable terminal record;
- restart after AgentRun success but before Job finalization can safely replay only the finalization projection, not model/tool execution;
- physical worker Job types are rejected;
- no B/C workflow is introduced.
