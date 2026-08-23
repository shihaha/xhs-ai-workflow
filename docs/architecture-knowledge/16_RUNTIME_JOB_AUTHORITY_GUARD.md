# 16 — Runtime ↔ Job Authority Guard

- Date: 2026-08-23
- Status: PR #9 dedicated gate passed; latest broad regression pending
- Scope: Agent Runtime / durable Job authority only
- Explicit non-scope: B/C workflow design, Android/XHS/browser physical worker ownership

## 1. Preconditions already proven

PR #6 (`fix/agent-runtime-foldin-v1`) passed its dedicated dynamic gate after GitHub Actions execution was restored:

- Stage 2 baseline backend capture;
- canonical Runtime compile;
- canonical Agent Runtime acceptance;
- canonical full-backend capture;
- Stage 2 -> canonical regression guard.

PR #7 (`spike/agent-job-binding-v1`) then passed:

- AgentRun ↔ Job binding compile/tests;
- existing Job state-machine tests;
- broader Agent Runtime backend/frontend/E2E/regression verification.

The authority model remains:

```text
Job       = authoritative task lifecycle / claim / lease / operator truth
AgentRun  = execution trace for one claim/continuation attempt
```

## 2. New hazard discovered after PR #7

A Job may own multiple historical AgentRun bindings:

```text
Job
  ├── AgentRun #1
  └── AgentRun #2  (later continuation)
```

After:

```text
running
 -> needs_human
 -> re-claim
 -> running
```

an old AgentRun is still durably bound to the same Job. Therefore this check alone is insufficient:

```text
run_id -> binding -> Job
Job.state == running
lease valid
```

Without an active-run rule, the historical AgentRun could appear authorized again when the Job is re-claimed for a newer continuation.

## 3. Active-run authority rule

For Job-bound Agent execution, authority requires all of the following:

1. `run_id` has a durable binding;
2. bound Job exists;
3. Job type is `agent_orchestration`;
4. Job state is `running`;
5. Job lease is present and unexpired;
6. AgentRun itself is still `running`;
7. AgentRun is the **single unambiguous latest binding** for that Job.

If the newest binding timestamp maps to more than one run, fail closed. Never choose a winner using UUID/string ordering.

This protects against an extreme same-timestamp collision and, more importantly, makes authorization independent of random identity ordering.

## 4. Runtime execution boundaries

A Job-bound Runtime must check authority at these boundaries:

```text
before model provider request
       ↓
provider request
       ↓
after model provider response
       ↓
permission / validation
       ↓
immediately before real Tool execution
```

### Why both sides of the model call?

A Job can be cancelled or lose its lease while the provider request is in flight.

If authority is checked only before the provider request, a stale `finish` or Tool decision could be applied after cancellation.

Therefore the post-model check is mandatory before persisting/applying the model decision as authoritative work.

### Why check again before Tool execution?

Permission evaluation is only a point-in-time decision. Job cancellation can happen after permission is granted but before the Tool handler starts.

`guard passed` is never a permanent authorization token.

Any future state-changing Job/domain command must still use its own write-boundary CAS/domain service semantics as well.

## 5. PR #9 staged adapter

Branch:

```text
spike/runtime-job-authority-guard-v1
```

Draft PR:

```text
#9 — Spike: enforce Job authority at Runtime boundaries
```

The spike deliberately uses an **unexported integration adapter** (`job_bound_runtime.py`) rather than modifying the accepted canonical Runtime immediately.

It provides:

- `ActiveRunJobAuthorityGuard`;
- model pre/post authority checks;
- Tool pre-execution authority check;
- `run_bound(run_id)` for an already-claimed/bound run;
- forbidden ordinary `start()` in Job-bound mode;
- forbidden old-run `resume()` / `resume_interrupted()` in Job-bound mode.

This prevents creation of an unbound AgentRun and prevents a historical run from being reopened after a continuation claim.

## 6. Dynamically proven focused behavior

Latest dedicated workflow for the hardened PR #9 head passes:

- compile;
- focused Runtime Job authority tests;
- complete `backend/tests/agent_runtime` acceptance;
- existing Job state-machine tests.

Focused tests prove:

1. cancelled Job before model => provider call count stays zero;
2. cancellation during model => stale finish result is not applied;
3. cancellation after permission => Tool handler call count stays zero;
4. expired lease => provider call count stays zero;
5. newer continuation => old AgentRun is superseded;
6. tied newest binding timestamps => all candidates fail closed;
7. non-running AgentRun => authority denied even if Job lease is valid;
8. Job-bound ordinary start/old-run resume are forbidden.

Do not mark the entire PR #9 accepted until its latest broad backend/E2E regression run also completes.

## 7. Human-wait projection is the next state boundary

The current Agent Runtime already persists HumanAction/checkpoint before returning `needs_human`.

For a bound run the next slice must project that fact into Job authority:

```text
AgentRun running
  -> persist HumanAction/checkpoint
  -> AgentRun needs_human
  -> CAS Job running -> needs_human
  -> clear Job lease
```

The ordering matters: the durable HumanAction must exist before the operator-visible Job is placed into `needs_human`.

### Crash window

A crash may occur after AgentRun/HumanAction persistence but before Job projection.

Recovery must therefore support this one-way reconciliation:

```text
latest AgentRun = needs_human
pending HumanAction exists
Job = running
    ↓
reconcile Job -> needs_human
clear lease
```

Recovery must **not** solve the conflict by reopening the AgentRun or replaying the pending Tool.

## 8. Continuation remains a separate problem

Do not casually call canonical `resume(..., resume_run=True)` for a Job-bound historical run.

The current accepted direction is:

```text
resolve HumanAction explicitly
Job needs_human
 -> re-claim Job
 -> create a new continuation AgentRun
```

But a real continuation implementation must also define how persisted budget/context/approval facts carry forward. Do not create a fresh unlimited run and call it a continuation by accident.

Therefore implement **wait projection + restart reconciliation first**, and treat approval/re-claim/context carry-forward as a following bounded slice.

## 9. Physical-worker boundary remains unchanged

None of this grants AgentRuntime ownership of:

- Android navigation;
- XHS collection loops;
- browser physical work;
- Qianfan/XHS worker lifecycle.

Those remain behind durable JobService/domain worker boundaries.

## 10. B/C scope remains unchanged

ADR-006 remains authoritative:

- no generic B product-research Agent;
- no generic C product-build Agent;
- no B/C state machine;
- only thin Product Definition and Finished Product + human UAT handoffs until repeated real product history justifies reusable automation.
