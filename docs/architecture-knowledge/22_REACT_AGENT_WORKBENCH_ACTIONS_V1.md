# 22 — React Agent Workbench Operator Actions V1

Status: implementation slice stacked on React Agent Workbench V1 / PR #18.

## Purpose

Add the first backend-authoritative operator commands to the Agent workbench without turning React into lifecycle authority and without pretending that continuation execution exists in FastAPI when it does not.

The durable authority chain remains:

`Agent -> durable Job / JobService -> physical worker -> Evidence / Artifact / Job state`

This slice does not add a Project, B/C workflows, direct Android/XHS/browser control, or an Agent execution daemon.

## Safe commands in this slice

### Cancel Agent Job

`POST /api/v1/agent-runtime/jobs/{job_id}/cancel`

Cancellation is a terminal operator command for `agent_orchestration` Jobs only.

The backend revalidates the observed Job state/retry/lease at the write boundary. A successful cancellation:

- sets the authoritative Job to `cancelled/operator_cancelled` and clears its lease;
- cancels only the current/latest execution trace(s), preserving older continuation Run history;
- resolves still-pending HumanActions as denied because a cancelled Job can never authorize a future continuation;
- records a durable Job log;
- preserves older continuation Run states as historical execution traces;
- is idempotent after the Job is already cancelled, while reconciling legacy stale current Run/HumanAction state if needed;
- never overwrites succeeded/failed terminal Jobs.

React uses a two-step confirmation UI. The button only requests the command; it does not mutate lifecycle locally.

### Deny a permission HumanAction

`POST /api/v1/agent-runtime/human-actions/{human_action_id}/deny`

The command is accepted only when durable backend facts prove that the exact HumanAction is:

- still pending;
- attached to a source AgentRun in `needs_human/approval_required`;
- attached to a lease-free `needs_human` Agent orchestration Job;
- attached to the single unambiguous latest Run binding.

A denial resolves the HumanAction to `denied`, appends a durable `human_denied` error step, and fail-closes the source AgentRun and Job to `failed/human_denied`, matching the existing bounded Runtime denial semantics.

Manual ChatGPT handoffs and other domain waits cannot use this permission-denial endpoint.

The generic HumanAction read projection now includes backend-derived `can_deny`. React displays the denial control only when this field is true; it never infers permission authority from tool names, labels, or raw payloads.

## Continuation remains deliberately unavailable

`GET /api/v1/agent-runtime/operator-capabilities` reports:

- cancel Job: available;
- deny permission action: available;
- approve continuation: unavailable.

The repository already contains a validated `JobContinuationCoordinator` that can atomically approve an exact HumanAction, reclaim the Job, create a continuation AgentRun, and carry remaining budget.

However, the FastAPI workbench application does not yet own a durable Agent continuation executor/model driver. Exposing approval now could create a legitimate `running` Job and continuation AgentRun with no process responsible for driving them. That would be a false operational state.

Therefore approval/continue/resume remains fail-closed until the executor lifecycle is implemented and proven restart-safe.

`result_ready=true` for manual ChatGPT handoff remains display state only and still does not authorize continuation.

## Generic Jobs API boundary

Generic `/api/v1/jobs/{id}/claim` and `/transition` reject `agent_orchestration` lifecycle mutation. Agent Job lifecycle commands must use the dedicated Agent Runtime authority surface.

Generic log/artifact persistence is not blocked because those are not lifecycle transitions and remain valid durable evidence/audit capabilities.

## UI rules

- cancellation requires a second explicit confirmation;
- permission denial requires a second explicit confirmation;
- denial appears only for backend-projected `can_deny=true`;
- ambiguous authority disables Job operator commands;
- no approve/continue/resume control is rendered;
- after a successful command the UI reloads durable backend projection rather than fabricating the resulting state.

## Verification

The dedicated workflow must cover:

- operator command contracts;
- existing Workbench read/projection/ChatGPT-handoff contracts;
- focused and complete React tests plus production build;
- complete Agent Runtime acceptance;
- compile/release-boundary checks.

Repository-wide `Agent Runtime Spike Verification` remains the broad regression gate. No merge is automatic.
