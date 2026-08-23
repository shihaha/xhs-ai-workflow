# 15 — AgentRun ↔ Durable Job Binding

- Date: 2026-08-23
- Status: DESIGN READY; implementation blocked on canonical Runtime dynamic acceptance
- Scope: runtime/task authority only; no B/C workflow design

## 1. Problem

The Agent Runtime spike currently persists `AgentRun` / `AgentStep` independently from the existing durable `JobService` state machine.

That separation was correct for the isolated spike, but it cannot remain ambiguous once Agent execution is exposed through the real workbench.

The project already has a proven Job authority with:

```text
queued
running
needs_human
succeeded
failed
cancelled
```

plus CAS claiming, leases, retry counts, logs, artifacts and fail-closed worker recovery.

The Agent Runtime adds a different kind of fact:

- model/tool execution trace;
- permissions;
- human actions;
- context/usage;
- checkpoints;
- exact runtime failure detail.

These are complementary. They must not become two competing task state machines.

## 2. Authority decision

**Job is the authoritative task lifecycle. AgentRun is an execution trace owned by a Job.**

```text
Job
  authoritative queue/claim/lease/operator lifecycle
  |
  └── AgentRun #1
        model/tool/permission/checkpoint trace
        |
        ├── AgentStep
        ├── HumanAction
        └── evidence refs

  retry / explicit continuation may create AgentRun #2
```

A Job may therefore own more than one AgentRun over time. An AgentRun belongs to at most one Job.

AgentRun must never become a second scheduler or silently override Job state.

## 3. Persistence shape

The Agent Runtime intentionally uses separate SQLAlchemy metadata from the production domain `Base`. Keep that isolation until there is a deliberate schema migration decision.

For the first integration, add a nullable indexed `job_id` string to `agent_runs` **without a database ForeignKey constraint across the two metadata registries**.

Integrity is enforced at the application/coordinator boundary:

- a bound run may only be created for an existing Job;
- a run's `job_id` is immutable after creation;
- the coordinator re-reads both records before every authoritative transition;
- orphaned bindings are an integrity error and fail closed.

This is preferable to forcing the experimental Agent metadata into the production declarative registry solely to obtain an FK.

Later production migration may add a physical FK if/when the Agent tables are intentionally promoted into the main schema lifecycle.

## 4. Creation must be coordinated

Do not implement this sequence naively:

```text
JobService.claim(job)
# process crashes here
AgentRunStore.create_run(job_id=job)
```

That creates a claimed/running Job with no execution trace.

The integration service should provide one transactional operation over the same SQLite engine/session:

```text
claim_job_and_create_agent_run(job_id, run_spec)
```

Semantics:

1. CAS Job from `queued|needs_human` -> `running`;
2. set/renew lease;
3. create the bound AgentRun;
4. commit both in one SQLite transaction;
5. return both durable identities.

SQLAlchemy mappings from separate declarative metadata can still participate in one Session/transaction because they share the same engine. Metadata separation is a DDL/registry boundary, not a transaction boundary.

If implementing one transaction proves awkward or unsafe in the current abstractions, stop and change the repository API rather than accepting a crash window.

## 5. Lease rule

Current defaults create a dangerous equality:

```text
Agent RunBudget.max_wall_time_seconds = 300
Job claim lease_seconds               = 300
```

An Agent run can therefore still be within its allowed runtime when the Job lease expires.

For a bound orchestration Job, require:

```text
job_lease_seconds > agent_max_wall_time_seconds + shutdown_margin_seconds
```

Initial proposed margin:

```text
shutdown_margin_seconds = 60
```

So a default 300-second Agent budget receives at least a 360-second Job lease.

Do not hard-code 360 independently of the run budget; derive it when claiming the Job.

V1 does not need a heartbeat if every Agent run is bounded below its derived lease and physical work is not executed inside the Agent process.

Revisit heartbeat/lease renewal only when a measured orchestration run must intentionally exceed the initial lease.

## 6. Critical physical-work rule

The current synchronous ToolRegistry deadline is cooperative: it checks before/after a handler, but it cannot forcibly interrupt a permanently blocked synchronous handler.

Therefore:

> **AgentRuntime must not directly own Android/XHS/browser physical-worker lifecycles.**

Do not expose raw tools such as:

```text
adb.navigate
xhs.open_note_and_scroll
android.collect_shop_directly
browser.do_physical_collection_in_agent_process
```

Instead preserve the proven durable worker boundary:

```text
Agent Runtime
   |
   | safe domain/job command
   v
JobService / domain service
   |
   | durable worker lifecycle
   v
Qianfan / XHS / Android / browser worker
   |
   v
Evidence / artifact / final Job state
```

The Agent may later:

- request/enqueue an allowed durable domain Job;
- read a Job;
- read its completed evidence/artifact/result;
- present a human action;
- decide the next safe orchestration step.

It must not replace the existing physical worker with a synchronous Tool handler.

## 7. State projection

AgentRun state is operational detail. Job state is operator/business task truth.

Expected projection for one bound orchestration run:

| AgentRun outcome | Required Job outcome |
|---|---|
| `running` | `running` |
| `needs_human` | `needs_human` |
| `succeeded` | `succeeded` |
| `failed` | `failed` |
| `cancelled` | `cancelled` |

But projection is not blind assignment. It must use JobService/CAS rules.

### Conflict rule

If Job and AgentRun disagree in a way that cannot be proven safe, **do not choose a winner by guess**.

Examples:

```text
Job = cancelled, AgentRun = running
→ stop further Agent work; Job terminal authority wins

Job = needs_human, AgentRun = running
→ no new model/tool step; reconcile/fail closed

Job = succeeded, AgentRun = running
→ integrity conflict; never continue the run

AgentRun = succeeded, Job = running
→ coordinator may attempt the allowed running→succeeded CAS
   only after required output/evidence persistence is complete
```

A failed CAS means state changed concurrently; re-read and fail closed rather than overwriting it.

## 8. Human wait/resume

When AgentRun requires a human:

1. persist HumanAction / Agent checkpoint first;
2. transition bound Job `running -> needs_human`;
3. clear Job lease using existing JobService semantics;
4. expose the human action in the workbench.

On approval/resume:

1. resolve the HumanAction explicitly;
2. claim the `needs_human` Job again (incrementing retry count under existing rules);
3. create a continuation AgentRun or explicitly resume the existing run according to the accepted runtime model;
4. restore the run's persisted budget/authority state;
5. never auto-replay an uncertain physical/external side effect.

For V1, prefer a **new continuation AgentRun for a re-claimed Job** when doing so improves audit clarity. If existing-run resume is retained, the binding must still record each Job re-claim/retry boundary in durable steps/events.

## 9. Cancellation

Job cancellation is authoritative.

Before every new model request and before every Tool execution boundary, a bound runtime must verify the Job is still `running` and the lease is valid.

If the Job is cancelled or no longer runnable:

- do not call the model again;
- do not execute the Tool;
- persist an Agent-side stop/cancel trace if safe;
- return control to the operator.

This requires a small project-owned run-control/job-authority guard rather than giving Tool handlers direct access to raw Job tables.

## 10. First implementation slice

After PR #6 dynamic acceptance, implement only:

```text
AgentRun.job_id (application-enforced reference)
AgentJobCoordinator
JobAuthorityGuard
job.read               # read-only Tool
```

Do **not** add real Android/XHS collection in this slice.

First vertical path:

```text
queued orchestration Job
 -> transactional claim + AgentRun creation
 -> Agent reads the bound Job
 -> deterministic/model-controlled finish
 -> coordinator CAS finalizes Job
 -> restart can re-read Job + AgentRun + steps
```

Then add human wait/resume and cancellation conflict tests.

## 11. Required acceptance tests

### Binding / transaction

1. missing Job cannot create a bound run;
2. claim + run creation succeeds atomically;
3. simulated failure before commit leaves neither half-transition committed;
4. run `job_id` cannot be rebound.

### Authority

5. Job cancellation blocks the next model step;
6. Job cancellation blocks the next Tool execution;
7. terminal Job can never be reopened by AgentRun;
8. Agent success only finalizes a currently running Job via allowed CAS;
9. CAS loss causes re-read/fail-closed, not overwrite.

### Lease

10. lease is derived from persisted Agent wall-time budget + margin;
11. default 300-second run never receives a 300-second-or-shorter lease;
12. human wait clears lease;
13. resume/re-claim establishes a new valid lease.

### Recovery

14. crash after atomic claim/run creation leaves both reconstructable;
15. stale Job lease recovery remains owned by JobService;
16. uncertain external/physical work is never auto-replayed by Agent recovery.

### Evidence / result

17. AgentRun steps preserve evidence refs;
18. Job final success is not written before required authoritative domain result/evidence is committed;
19. refresh/restart reconstructs the same Job↔Run relationship.

## 12. Explicit non-goals

This binding does not implement:

- B product-research workflow;
- C product-build workflow;
- generic B/C agents;
- physical device control inside AgentRuntime;
- distributed orchestration engine;
- multi-machine scheduler;
- automatic publication.

## 13. Exit gate

The binding is accepted only when:

- canonical Runtime dynamic tests are green first;
- all binding tests above are green;
- existing JobService regression baseline has no new reproducible failures;
- one controlled read-only orchestration Job survives restart/reconstruction;
- Job remains the single authoritative lifecycle visible to the operator.

Until then, `AgentRun ↔ Job` is a design contract, not production behavior.