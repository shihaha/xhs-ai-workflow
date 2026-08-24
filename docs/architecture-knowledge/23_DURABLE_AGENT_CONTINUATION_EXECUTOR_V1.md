# 23 — Durable Agent Continuation Executor V1

Status: implementation slice stacked on React Agent Workbench Operator Actions V1 / PR #19.

> Provider note (2026-08-24): the Bailian-only admission/configuration assumptions recorded in this historical V1 slice are superseded for new work by `26_CHATGPT_PRIMARY_REASONING_PROVIDER.md`. Keep the restart/no-replay durability behavior; do not keep Bailian as a mandatory reasoning dependency.

## Purpose

Turn an exact approved permission HumanAction into real bounded Agent continuation execution without allowing React, an in-memory queue, or a physical Android/XHS/browser worker to become lifecycle authority.

The authority chain remains:

`Agent -> durable Job / JobService -> physical worker -> Evidence / Artifact / Job state`

This slice adds no Project entity, no generic B/C workflow, and no direct Agent ownership of Android/XHS/browser processes.

## Approval and dispatch transaction

`POST /api/v1/agent-runtime/human-actions/{human_action_id}/approve`

Approval is accepted only for the existing strict `needs_human/approval_required` permission path. Manual ChatGPT waits and other domain waits remain excluded.

`JobContinuationCoordinator.approve_and_create_continuation(...)` now commits all of the following in one SQLite transaction:

1. resolve the exact pending HumanAction as approved;
2. CAS the authoritative Job from lease-free `needs_human` back to `running`;
3. increment the Job claim generation and assign a bounded lease;
4. create a new child AgentRun with remaining Job-level budget;
5. bind the child AgentRun to the same Job;
6. persist the exact source-run / HumanAction / tool-call continuation link;
7. create an `agent_continuation_dispatches` row in `queued` state.

The dispatch row is important: a successful approval can never depend only on an in-memory queue notification. If the process exits immediately after commit, a later executor instance can rediscover the durable queued child run.

The old source AgentRun remains historical `needs_human`; continuation never mutates it back to running.

## App-owned executor

`AgentContinuationExecutor` is owned by the FastAPI application lifespan.

It consumes only durable continuation dispatch rows. Before executing a child run it must prove that:

- the child AgentRun is the unique current Job binding;
- the Job is still `running`;
- the Job still has a valid running lease;
- the AgentRun itself is still `running`.

Dispatch acquisition is a CAS from `queued` to `running`. The executor then calls the already-proven `JobBoundAgentRuntime.run_approved_continuation(run_id)` path.

The Runtime executes the exact durable approved Tool action first and then resumes the bounded model loop. Existing Runtime guards remain authoritative for tool-call identity conflicts, committed-result reuse, uncertain side effects, Job authority loss, budget exhaustion, human wait projection, and terminal Job projection.

## Restart behavior

The local workbench is a single FastAPI process. At executor startup, any dispatch left `running` by the previous process is returned to `queued` for durable recovery.

Re-driving does not mean blindly replaying a Tool:

- if the exact approved Tool result was already durably committed, the Runtime skips handler replay and continues the model loop;
- if a prior execution is uncertain / started without a safe committed outcome, the canonical Runtime fail-closes to `needs_human` rather than replaying the side effect;
- if the AgentRun already durably reached `needs_human` but the process died before Job projection, restart replays only the safe wait projection;
- if the AgentRun already durably reached `succeeded`/`failed` but the process died before terminal Job projection, restart replays only the terminal projection from durable proof;
- if Job authority was lost, the executor does not create new authority and reconciles only through existing durable recovery/projection paths.

Shutdown closes new approval admission before database disposal and waits for the bounded active continuation to reach a durable Runtime boundary.

## Production model and Tool surface

The app always owns the durable executor so restart reconciliation can consume old dispatch rows even if model configuration later disappears. **New approval admission** is enabled only when the existing Bailian text-model configuration has a non-empty API key. With no model configuration the executor rejects new approvals and fail-closes any already-durable queued/running continuation back to a human-review boundary instead of leaving a false `running` Job.
When automatic execution is unconfigured and there is no queued/running continuation to reconcile, startup does not create an idle polling thread. This preserves worker isolation while retaining restart recovery whenever durable continuation work actually exists.

The production Job-bound Agent reuses the existing `BailianModelAdapter`, including its bounded retry/timeout/error-normalization path. Agent Runtime does not create a second provider client. No credential is persisted into AgentRun/Step/dispatch/workbench rows.

V1 exposes only already-owned deterministic Agent tools:

- `job.read`
- `analysis.run_grounded`

It deliberately does **not** expose Android, XHS, browser, Qianfan, shop-collection, or other physical worker control as synchronous Agent tools. Those workflows must continue through durable Jobs/services/workers.

If the model/executor is not configured, `/operator-capabilities` reports `approve_continuation=false` and approval remains fail-closed.

## Workbench UI rules

The HumanAction projection includes backend-derived `can_approve` alongside `can_deny`.

React renders `批准并继续` only when both are true:

1. the exact HumanAction projection says `can_approve=true` from durable authority facts; and
2. `/operator-capabilities` says the app-owned continuation executor is available.

Approval requires a second explicit confirmation (`确认批准并继续`). React sends the command and reloads durable projections; it never locally changes Job or AgentRun state.

The approval response reports that the child continuation was durably enqueued. It does not claim a terminal result because the executor may still be running.

Manual ChatGPT `result_ready=true` remains a separate handoff lifecycle fact and does not silently enter this permission-approval path.

## Verification requirements

The dedicated workflow for this slice must cover:

- approval + durable dispatch transaction;
- real executor execution with a deterministic fake model/tool;
- restart rediscovery of a durable running dispatch without reapproval;
- manual ChatGPT exclusion;
- production configuration gate and restricted Tool surface;
- prior Job-bound continuation and Workbench operator/read/handoff contracts;
- focused and complete React tests plus production build;
- complete Agent Runtime acceptance;
- an executor-specific FastAPI lifespan coexistence check proving existing app-owned workers are not displaced;
- Python compile and release-boundary scan.

Repository-wide `Agent Runtime Spike Verification` remains the broad baseline-vs-spike regression gate. No PR is merged automatically.
