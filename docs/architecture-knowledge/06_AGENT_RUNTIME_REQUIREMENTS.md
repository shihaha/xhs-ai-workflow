# 06 — Agent Runtime Requirements

- Version: research baseline v1
- Date: 2026-08-23
- Status: requirements for Phase 3 candidate evaluation; not implementation authorization

## 1. Runtime mission

The Agent Runtime must add model-driven orchestration **above** the current durable domain core without weakening evidence integrity, business gates or failure semantics.

It must answer:

> Given a business goal, current durable state, relevant trusted evidence and currently available tools, what bounded action should happen next?

It must not answer:

> What facts should be considered true regardless of the evidence system?

## 2. Non-negotiable invariants

The runtime must preserve:

- SQLite as authoritative V1 business database unless a later ADR changes this;
- existing Job state semantics;
- `running` interruption -> explicit recovery / `needs_human` where currently required;
- immutable evidence/artifact/audit history;
- current evidence eligibility and revalidation rules;
- Opportunity human review;
- separate Product Definition human gate before product build;
- Finished Product/UAT gate before content production;
- no automatic publishing/liking/favoriting/commenting;
- no fabricated selectors, URLs, evidence, progress, success or business metrics.

## 3. Required runtime entities

### AgentRun

Minimum durable fields:

```text
id
job_id / project_id
parent_run_id? / conversation_id?
agent_type
runtime_version
goal
state
model_provider
model
prompt_version
started_at
updated_at
completed_at?
step_count
model_call_count
input_tokens
output_tokens
estimated_or_actual_cost?
wall_time_ms
budget_json
last_checkpoint_step?
error_category?
error_detail?
```

Recommended states:

```text
queued
running
waiting_human
succeeded
failed
cancelled
budget_exhausted
stuck
```

These are runtime states and do not replace the outer business Job state machine.

### AgentStep

Minimum durable fields:

```text
id
agent_run_id
step_index
kind
status
model_or_tool_name?
validated_input_json?
output_json?
evidence_refs_json
permission_decision_id?
usage_json
duration_ms
idempotency_key?
error_category?
created_at
completed_at?
```

Kinds should cover at least:

```text
model
permission
tool
checkpoint
summary
human_action
```

### PermissionDecision / HumanAction

Persist separately when useful for audit and UI:

```text
target run/step/tool
decision
reason/source
requested args digest
actor/authority reference?
created_at
resolved_at?
resolution?
```

## 4. Model/action contract

The runtime must not parse free-form prose to decide which sensitive action to execute.

Each model turn must produce one of a small structured outcomes, for example:

```text
CallTool(tool_name, arguments, rationale_summary?)
Finish(output)
NeedHuman(reason, requested_decision?)
```

Requirements:

- strict schema validation;
- unknown fields rejected;
- unknown/unavailable tools rejected;
- malformed actions never partially execute;
- final outputs validated where a domain schema exists;
- the model cannot directly set trusted business status fields.

## 5. Tool Registry

Every Agent-visible tool must register:

```text
name
version
description
input_schema
output_schema
availability/prerequisites
read_only
destructive
external_side_effect
concurrency_safe
permission_class
timeout
retry_policy
idempotency_policy
cost_class
execute
```

### Required behavior

- tool names are stable and namespaced;
- duplicate names/versions fail startup/registration;
- schemas are generated from trusted code, not model text;
- availability is recomputed from current trusted environment/state;
- unavailable tools are omitted or marked unavailable before model choice;
- tool output is size-bounded;
- large outputs can be spilled into managed runtime storage and represented by typed references;
- direct SQL/browser/ADB/shell access is not a normal Agent tool.

## 6. PermissionPolicy

Required canonical decisions:

```text
allow
allow_with_budget
ask
deny
```

Decision input should include:

```text
agent role
tool identity/version
validated args
current project/stage/job/run state
tool metadata
prerequisite status
existing human approvals
budget state
side-effect class
```

### Required behavior

- deny beats allow;
- business gates cannot be overridden by prompt instructions;
- `ask` in non-interactive execution becomes a durable `waiting_human/needs_human` condition;
- decision reason/source is persisted;
- approval must be bound to a specific operation/version/arguments or a clearly defined broader rule;
- stale approvals cannot authorize materially changed arguments;
- approval does not replace server-side authorization/evidence validation.

## 7. Bounded execution

Every AgentRun requires explicit limits:

```text
max_steps
max_model_calls
max_input_tokens
max_output_tokens
max_wall_time
max_consecutive_failures
max_tool_output_bytes
per_tool_timeout
per_tool_retry_limit
```

Optional later:

```text
max_cost
per_agent/delegate budgets
max_external_actions
```

Budget exhaustion must produce a durable explicit state. It must not create an infinite self-retry loop.

## 8. Retry rules

Different failures need different retry semantics.

### Potential automatic retry

Only when all are true:

- operation is safe/idempotent or explicitly retryable;
- failure category is transient;
- retry budget remains;
- prerequisites remain valid;
- retry will be recorded as a new attempt/step.

### Never blind-auto-retry

- physical Android navigation after uncertain interruption;
- browser operation whose external state is unknown;
- human-denied operation;
- evidence-grounding failure caused by invalid facts/output;
- business gate denial;
- immutable-write conflict;
- malformed tool/action that requires model correction rather than replay.

## 9. Checkpoint requirements

A checkpoint is a durable statement of committed orchestration state.

After each successful state-relevant tool step, persist enough to reconstruct:

- current goal/stage;
- completed step IDs;
- current tool/evidence outputs;
- compact operational summary;
- budgets consumed;
- pending human action, if any;
- next safe boundary.

Checkpointing must happen **after** authoritative domain persistence is known to have landed.

For uncertain DB/filesystem commits, preserve existing fail-safe classification rather than creating a false checkpoint.

## 10. Resume requirements

Resume must:

1. load the last proven checkpoint;
2. re-read current Job/domain state;
3. re-check tool availability/prerequisites;
4. re-check relevant human approvals;
5. avoid repeating already committed idempotent outputs;
6. require explicit operator intervention for unsafe interrupted physical actions;
7. create a new durable continuation/run identity when that improves auditability.

Conversation history alone is insufficient to prove what happened externally.

## 11. Context Builder requirements

The runtime must expose a dedicated Context Builder.

Inputs:

```text
agent role/current goal
relevant business rules
current project/job/run state
selected trusted facts/evidence refs
recent operational steps/checkpoint
currently available tool definitions
budget/context usage
```

### Required guarantees

- irrelevant evidence is not included by default;
- every compact business fact is traceable to authoritative evidence IDs/records;
- compact projections are recomputable and non-authoritative;
- raw evidence remains available outside model context;
- prompt/tool schema size is measurable before provider call;
- tool definitions can be lazy/on-demand where useful;
- model-based summarization cannot remove required phase/evidence constraints;
- no hidden chain-of-thought is required for resume.

## 12. Context optimization acceptance benchmark

Use the real successful cross-account analysis as a fixed benchmark:

```text
2 accounts
22 eligible evidence facts
243,276 prompt tokens historically
```

A future optimization is acceptable only if:

- same evidence set and business contract remain enforceable;
- output/evidence grounding remains equal or stronger;
- successful result remains traceable to exact evidence IDs;
- current-evidence revalidation remains intact;
- input-token usage is materially lower on fixed replay;
- no trusted fact is created solely by a compact summary.

## 13. Tool output management

Required cheap-first controls:

- tool-specific output schemas;
- maximum inline bytes/items;
- pagination/query handles for large results;
- managed file/artifact references;
- deduplicate/supersede old operational reads;
- recent-tail preservation;
- optional summarization only after deterministic reductions;
- preserve tool-call/result semantic pairing in model history.

## 14. Hooks and observability

Runtime must provide stable lifecycle hooks/events at least for:

```text
run_started
before_model
after_model
before_tool_validate
after_tool_validate
permission_decision
before_tool
after_tool
checkpoint_committed
human_action_required
run_finished
run_error
```

Hooks are for logging, metrics, validation and projections. They must not become an unbounded plugin mechanism in V1.

## 15. Usage and cost accounting

Per run and per model/tool step record where available:

- input/output tokens;
- model/provider;
- duration;
- retries/attempts;
- estimated/actual cost if reliably available;
- context usage fraction if resolvable.

UI must be able to show these without parsing log strings.

## 16. Stuck detection

The runtime should detect patterns such as:

- repeated same tool + materially same arguments;
- repeated same failure category;
- no durable progress across N steps;
- repeated model correction loops;
- context/budget near exhaustion.

Stuck detection should stop or request human help; it must not silently declare success.

## 17. Specialized agents

V1 requirement: a single orchestrator can run without sub-agents.

If sub-agents are later enabled:

- each has explicit allowed tools;
- own step/time/token/call budget;
- independent context by default;
- no implicit destructive-tool inheritance;
- events and usage flow to parent observability;
- child cannot grant its own permissions/business approvals;
- recursion/nested delegation is off by default.

## 18. MCP / external tools

MCP support is desirable but not a prerequisite for the first runtime slice.

Any MCP tool must still satisfy:

- connection/capability availability;
- schema validation;
- PermissionPolicy;
- timeout/budget;
- output bounds;
- secret handling;
- domain ingestion/validation before becoming trusted evidence.

## 19. Security / privacy

- never persist API keys, cookies, passwords or auth tokens in AgentStep outputs;
- tool results pass through redaction where appropriate;
- runtime filesystem access is constrained to managed roots;
- UI/event APIs expose safe projections, not arbitrary local paths/secrets;
- model prompts should contain the minimum sensitive data required.

## 20. Minimum implementation slice

The first implementation experiment should include only:

1. AgentRun/AgentStep types + persistence;
2. Tool protocol/registry;
3. PermissionPolicy;
4. bounded loop with fake model + fake tools;
5. checkpoint/resume semantics;
6. lifecycle events/usage counters;
7. tests.

Do **not** initially connect Android, Qianfan, XHS browser, Opportunity approval, product creation or publishing.

## 21. Acceptance tests for any runtime candidate

A candidate/runtime implementation must demonstrate:

- malformed model action is rejected before tool execution;
- unavailable tool cannot execute;
- phase/business denial cannot be bypassed by model prompt;
- headless `ask` becomes durable human wait;
- max-step/model/token/time limits stop safely;
- tool timeout is categorized and durable;
- committed checkpoint reconstructs correctly;
- explicit resume skips already committed idempotent work;
- unsafe physical action is not auto-replayed;
- large tool output does not explode context;
- raw evidence trust rules remain unchanged;
- compact context cannot create a trusted fact;
- Agent cannot approve its own Opportunity/Product Definition;
- no Phase B/C/D transition occurs without explicit authority.

These requirements become part of the Phase 3 open-source evaluation matrix.