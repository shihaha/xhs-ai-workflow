# 15 — AgentRun ↔ Durable Job Binding

- Date: 2026-08-23
- Status: DESIGN READY; implementation may proceed only as stacked draft until canonical Runtime dynamic acceptance
- Scope: runtime/task authority only; no B/C workflow design

## 1. Problem

The Agent Runtime spike persists `AgentRun` / `AgentStep` independently from the existing durable `JobService` state machine.

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
  ├── AgentRun #1
  └── AgentRun #2 (retry/continuation, if needed)
```

A Job may own more than one AgentRun over time. An AgentRun belongs to at most one Job.

AgentRun must never become a second scheduler or silently override Job state.

## 3. Persistence shape

The Agent Runtime intentionally uses separate SQLAlchemy metadata from the production domain `Base`. Keep that isolation until there is a deliberate schema-promotion decision.

The first integration should **not mutate the existing `agent_runs` table just to add a column**, because the spike already creates that table with `checkfirst=True` and does not yet have an Agent-table migration system.

Instead add one thin relationship table:

```text
agent_job_bindings
  run_id      PK / immutable AgentRun identity
  job_id      indexed Job identity
  created_at
```

The table is deliberately small and application-enforced. It may use its own explicit metadata/table creation, like the other spike tables.

Integrity rules:

- a binding may only be created for an existing Job and AgentRun inside the coordinator transaction;
- `run_id` is unique/immutable, so one run cannot be rebound;
- one Job may have multiple run bindings over retries/continuations;
- the coordinator re-reads both sides before authoritative transitions;
- an orphaned binding is an integrity error and fails closed.

A later production migration may collapse this into `agent_runs.job_id` with a physical FK if/when Agent persistence is intentionally promoted into the main schema lifecycle. Do not force that migration during the spike.

## 4. Creation must be coordinated

Do not implement this sequence naively:

```text
JobService.claim(job)
# process crashes here
AgentRunStore.create_run(...)
```

That creates a claimed/running Job with no execution trace.

The integration service should provide one transactional operation over the same SQLite engine/session:

```text
claim_job_and_create_agent_run(job_id, run_spec)
```

Semantics:

1. CAS Job from `queued|needs_human` -> `running`;
2. set/renew lease;
3. create AgentRun;
4. create `agent_job_bindings` row;
5. optionally append a Job audit log;
6. commit all records in one SQLite transaction;
7. return both durable identities.

SQLAlchemy mappings from separate declarative metadata can participate in one Session/transaction because they share the same engine. Metadata separation is a DDL/registry boundary, not a transaction boundary.

If one transaction cannot be proven, stop and change the repository API rather than accepting a crash window.

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

Initial margin:

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

The Agent may later request/enqueue an allowed durable domain Job, read its state, read completed evidence/artifacts/results, present a human action, and decide the next safe orchestration step.

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

Projection is not blind assignment. It must use JobService/CAS rules.

### Conflict rule

If Job and AgentRun disagree in a way that cannot be proven safe, **do not choose a winner by guess**.

```text
Job = cancelled, AgentRun = running
→ stop further Agent work; Job terminal authority wins

Job = needs_human, AgentRun = running
→ no new model/tool step; reconcile/fail closed

Job = succeeded, AgentRun = running
→ integrity conflict; never continue the run

AgentRun = succeeded, Job = running
→ coordinator may attempt running→succeeded CAS only after required result/evidence persistence
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
2. claim the `needs_human` Job again;
3. create a continuation AgentRun or explicitly resume the existing run according to the accepted runtime model;
4. restore persisted budget/authority state;
5. never auto-replay an uncertain physical/external side effect.

For V1, prefer a **new continuation AgentRun for a re-claimed Job** when doing so improves audit clarity. If existing-run resume is retained, record each Job re-claim/retry boundary durably.

## 9. Cancellation

Job cancellation is authoritative.

Before every new model request and before every Tool execution boundary, a bound runtime must verify the Job is still `running` and the lease is valid.

If the Job is cancelled or no longer runnable:

- do not call the model again;
- do not execute the Tool;
- persist an Agent-side stop/cancel trace if safe;
- return control to the operator.

This requires a project-owned run-control/job-authority guard rather than giving Tool handlers direct raw-table access.

## 10. First implementation slice

A stacked Draft branch may implement the persistence/transaction contract before PR #6 is dynamically green, but it is **not merge-eligible** until the parent Runtime gate passes.

First slice only:

```text
agent_job_bindings table
AgentJobCoordinator
job.read read-only Tool
transaction/lease tests
```

Do **not** modify the canonical Runtime loop or connect Android/XHS collection in this slice.

First vertical path after parent acceptance:

```text
queued orchestration Job
 -> transactional claim + AgentRun + binding creation
 -> Agent reads the bound Job
 -> deterministic/model-controlled finish
 -> coordinator CAS finalizes Job
 -> restart can re-read Job + AgentRun + binding + steps
```

Then add the JobAuthorityGuard at model/tool boundaries, followed by human wait/resume and cancellation conflict tests.

## 11. Required acceptance tests

### Binding / transaction

1. missing Job cannot create a bound run;
2. claim + run + binding creation succeeds atomically;
3. injected persistence conflict rolls back the Job claim too;
4. run cannot be rebound to a different Job;
5. one Job may own multiple continuation runs.

### Authority

6. Job cancellation blocks the next model step;
7. Job cancellation blocks the next Tool execution;
8. terminal Job can never be reopened by AgentRun;
9. Agent success only finalizes a currently running Job via allowed CAS;
10. CAS loss causes re-read/fail-closed, not overwrite.

### Lease

11. lease derives from Agent wall-time budget + margin;
12. default 300-second run never receives a 300-second-or-shorter lease;
13. human wait clears lease;
14. resume/re-claim establishes a new valid lease.

### Recovery

15. crash after atomic claim/run/binding creation leaves all three reconstructable;
16. stale Job lease recovery remains owned by JobService;
17. uncertain external/physical work is never auto-replayed by Agent recovery.

### Evidence / result

18. AgentRun steps preserve evidence refs;
19. Job final success is not written before required authoritative domain result/evidence is committed;
20. refresh/restart reconstructs the same Job↔Run relationship.

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
- all binding tests are green;
- existing JobService regression baseline has no new reproducible failures;
- one controlled read-only orchestration Job survives restart/reconstruction;
- Job remains the single authoritative lifecycle visible to the operator.

Until then, `AgentRun ↔ Job` is a design/staged-spike contract, not production behavior.