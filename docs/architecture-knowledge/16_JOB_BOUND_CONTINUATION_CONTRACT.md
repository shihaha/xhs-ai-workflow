# Job-bound Continuation Contract

Status: research / implementation contract

This document defines the next bounded slice after Job-bound human-wait projection. It does not authorize merge of any spike and does not change the B/C scope override.

## Authority model

Job remains the authoritative lifecycle. AgentRun remains execution trace. A human-wait continuation never reopens the historical waiting AgentRun. Approval must create a new AgentRun bound to the same `agent_orchestration` Job after a guarded `needs_human -> running` re-claim.

## Exact approval object

A permission-gated wait already persists a HumanAction containing the original `tool_call_id`, tool name, and validated arguments. Human approval applies only to that exact persisted action.

The continuation must not ask the model to regenerate or reinterpret the approved action before execution. Otherwise the object that was approved could silently change.

Safe sequence:

```text
old AgentRun needs_human
  + pending HumanAction(action A)
  + Job needs_human
        |
        | human approves exactly A
        v
atomic approval + Job re-claim + new AgentRun/binding
        |
        v
new AgentRun executes exactly A under current Job authority
        |
        v
normal bounded model loop may continue
```

A denied HumanAction must never create a continuation run.

## Domain waits are not binary approvals

`ToolNeedsHumanError` can place an AgentRun in `needs_human` without a pending HumanAction. That state must not be routed through the binary approve/deny continuation path.

A later slice may add an explicit domain-resolution contract. Until then, domain waits remain manual/fail-closed.

## Budget carry-forward

A continuation must not reset the original effective step/model/token budget.

Use the oldest AgentRun bound to the Job as the root budget policy. Before creating a continuation, sum durable consumption across every historical AgentRun already bound to the Job.

Conceptually:

```text
remaining_steps        = root.max_steps        - sum(run.step_count)
remaining_model_calls  = root.max_model_calls  - sum(run.model_calls)
remaining_input_tokens = root.max_input_tokens - sum(run.input_tokens)
remaining_output_tokens= root.max_output_tokens- sum(run.output_tokens)
```

If any required remaining allowance is exhausted, do not re-claim the Job merely to create a run that cannot execute. Fail closed and surface budget exhaustion for human handling.

Wall-time is different: the existing runtime measures it from one AgentRun's `created_at`. Human waiting time must not silently consume token/step/model budget. The continuation slice should preserve the original wall-time policy per active claim unless/until durable active-execution time is modeled separately.

## Context carry-forward

A fresh continuation AgentRun has no local steps, but the model still needs bounded historical context.

Do not copy old AgentStep rows into the new run: copying would distort durable usage/step accounting and duplicate audit records.

Preferred approach:

- resolve the Job from the new run binding;
- read historical run IDs for that Job in durable binding order;
- construct bounded recent context across those historical runs plus the new run;
- preserve evidence references and bounded summaries;
- present a monotonic in-memory context order without mutating persisted step indices.

Historical context is read-only input to the model, not new execution history.

## Atomic approval / claim boundary

The following must be one SQLite transaction where feasible:

1. verify Job is `agent_orchestration` and `needs_human`;
2. verify the waiting AgentRun is the single unambiguous latest binding;
3. verify that run is durably `needs_human`;
4. verify one pending HumanAction belongs to that run;
5. resolve that exact HumanAction as approved;
6. CAS Job `needs_human -> running` and increment retry count;
7. create the new AgentRun with remaining budget;
8. create its Job binding;
9. write an audit Job log.

If any insert/update fails, none of the above should commit.

## Restart / replay rule

Approval and re-claim can commit before the approved Tool executes. Therefore restart handling must distinguish:

- new continuation exists, approved action durable, no Tool step for that call in the continuation: action has not started and may be executed after re-validation/authority check;
- Tool step is durably `started`/`uncertain`: do not automatically replay a potentially side-effecting action;
- Tool result is durably committed: reuse only under the existing idempotency rules.

Never infer safety from process memory.

## Execution boundaries

Before executing the approved Tool, the continuation must re-check current Job authority at the Tool write boundary. A point-in-time approval is not a durable capability.

Physical Android/XHS/browser work remains behind durable domain Jobs/workers. The Agent runtime does not own those worker lifecycles.

## Non-scope

This contract does not introduce:

- B/C workflow automation;
- ProductResearchAgent or ProductBuildAgent;
- direct Android/XHS/browser control;
- terminal AgentRun -> Job success/failure projection;
- a universal domain-wait resolver.

## Acceptance for the next implementation slice

At minimum prove:

- approval binds to exact persisted `tool_call_id/tool/args`;
- denial creates no continuation;
- stale/old run cannot approve after a newer binding exists;
- approval + re-claim + new run/binding is atomic;
- continuation receives only remaining step/model/token budget;
- exhausted budget prevents re-claim;
- new run sees bounded historical Job context without copied AgentStep rows;
- crash after approval/re-claim but before Tool start is recoverable;
- started/uncertain side-effect Tool is never blindly replayed;
- cancellation/lease loss before approved Tool execution fails closed.
