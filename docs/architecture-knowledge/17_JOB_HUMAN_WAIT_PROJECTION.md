# 17 — Job-bound Human Wait Projection

- Date: 2026-08-23
- Status: implementation spike PR #10; latest focused/broad gates pending
- Parent: PR #9 Runtime Job Authority Guard — dynamically accepted
- Scope: AgentRun `needs_human` -> authoritative Job wait projection and restart reconciliation
- Explicit non-scope: approval/re-claim continuation, terminal result projection, B/C workflow, physical worker ownership

## 1. Required ordering

For a Job-bound run that needs operator intervention:

```text
AgentRun running
  -> persist permission/domain wait facts
  -> persist checkpoint where applicable
  -> AgentRun needs_human
  -> CAS Job running -> needs_human
  -> clear Job lease
```

The Agent wait must be durable before operator-visible Job authority is tightened.

## 2. Crash window

A process may die after Agent persistence but before Job projection:

```text
AgentRun = needs_human
Job      = running + lease
```

This is recoverable only in the safe direction:

```text
re-open database
 -> verify run is still the current bound run
 -> verify AgentRun is needs_human
 -> project Job running -> needs_human
 -> clear lease
```

Never recover by reopening the old AgentRun or replaying its pending Tool.

The same projector must be usable for normal return and restart reconciliation so the two paths cannot drift semantically.

## 3. HumanAction nuance

Permission approval waits already create a durable `HumanAction` before AgentRun enters `needs_human`.

Existing domain Tool control may also produce `needs_human` without a binary approve/deny HumanAction. A selector-change/operator-intervention state is not necessarily a yes/no approval question.

Therefore wait projection must not invent a fake approval action merely to satisfy a UI shape. It may project a durable domain wait into Job authority using the AgentRun/checkpoint/error facts already persisted.

## 4. Terminal Job wins

If Job cancellation/finalization wins before wait projection:

```text
AgentRun = needs_human
Job      = cancelled|failed|succeeded
```

then Job terminal authority wins. Projection must not reopen or overwrite it.

This may leave an Agent-side historical mismatch for audit, but it is safe because no further Agent execution authority exists.

## 5. Idempotency

Repeated restart reconciliation is valid:

```text
Job = needs_human
lease = null
AgentRun = needs_human
```

A second projection is a no-op.

Impossible corruption such as:

```text
Job = needs_human
lease != null
```

must fail closed rather than silently repairing raw database fields.

## 6. TOCTOU #1 — new claim after Job read

Unsafe sequence:

```text
old projector verifies old run
old projector reads Job claim generation N
        ↓
other process projects wait
other process re-claims Job as generation N+1
        ↓
old projector writes using only Job.state == running
```

A state-only CAS would clobber the new continuation.

Minimum defense: write CAS must match the observed claim generation, including current `retry_count` and `lease_expires_at`.

## 7. TOCTOU #2 — new binding after guard, before Job read

Claim-generation matching alone is still insufficient.

Unsafe sequence:

```text
old projector passes current-binding guard
        ↓
other process projects + re-claims
new AgentRun binding is committed
        ↓
old projector reads the NEW retry_count + lease
        ↓
old projector could now match the new claim generation
```

Therefore the **state-changing SQL statement itself** must also prove:

1. `run_id` is still a binding for this Job;
2. its binding timestamp is the newest timestamp;
3. exactly one binding owns that newest timestamp.

The write boundary, not a prior read, is the final authority check.

Target predicate conceptually:

```text
UPDATE jobs
SET state = needs_human, lease = null
WHERE
  job.id = bound_job
  AND job.type = agent_orchestration
  AND job.state = running
  AND job.retry_count = observed_retry_count
  AND job.lease_expires_at = observed_lease
  AND current_run_is_unique_latest_binding_at_update_time
```

If any predicate loses, re-read authoritative Job state and fail closed unless another projector already produced the identical safe `needs_human + no lease` state or a terminal Job won.

## 8. Why a generic JobService state-only transition is insufficient here

`JobService.transition` correctly protects the generic Job state machine with a state CAS.

The Agent/Job integration boundary has one additional cross-table invariant: **which AgentRun owns the current Job claim**.

Therefore this narrow integration projector needs a claim+binding-aware CAS. This does not create a second scheduler; it tightens one permitted `running -> needs_human` edge using stricter preconditions.

## 9. PR #10 focused acceptance requirements

Tests must cover:

1. permission wait persists HumanAction before Job wait projection;
2. Job moves running -> needs_human and lease clears;
3. restart repairs Agent-wait/Job-running crash window;
4. repeated reconcile is idempotent;
5. domain needs_human without binary HumanAction still tightens Job authority;
6. terminal Job wins a race;
7. old superseded run cannot project after new continuation exists;
8. missing projector never silently returns a Job-bound needs_human result;
9. needs_human + live lease corruption fails closed;
10. re-claim after stale projector reads old Job prevents stale write;
11. re-claim after initial binding guard but before Job read also prevents stale write.

## 10. Continuation deliberately deferred

This slice does not implement:

```text
resolve HumanAction
 -> re-claim Job
 -> new continuation AgentRun
 -> restore context/budget/approval facts
```

A naive fresh AgentRun would accidentally reset budget/context and may lose audit semantics. Continuation carry-forward must be designed and tested separately.

## 11. Physical/B-C boundaries unchanged

AgentRuntime still does not own Android/XHS/browser physical worker lifecycles.

ADR-006 still forbids speculative generic B/C workflows until repeated heterogeneous product history demonstrates reusable patterns.
