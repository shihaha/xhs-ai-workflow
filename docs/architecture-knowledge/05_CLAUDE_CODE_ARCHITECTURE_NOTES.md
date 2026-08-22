# 05 — Claude Code Architecture Notes for XHS Workbench Next

- Derived: 2026-08-23
- Purpose: translate mature Claude Code architecture observations into **project-specific design lessons**
- Legal boundary: reconstructed Claude Code 2.1.88 source is study material only; no source copying/vendor dependency

## 1. The important thing Claude Code gets right

Claude Code is not impressive because it has many tools. Its core strength is that **model autonomy is surrounded by deterministic infrastructure**.

The useful abstraction for us is:

```text
Goal + bounded context
        ↓
      Model
        ↓
Structured next action
        ↓
Schema validation
        ↓
Tool availability + PermissionPolicy
        ↓
Deterministic domain tool
        ↓
Durable result/evidence/checkpoint
        ↓
Next bounded context
```

That structure is more important than the exact prompt, model, terminal UI or coding tools.

## 2. Model is planner/operator, not source of truth

In our XHS system the Agent may decide **which allowed action to request next**, but it does not decide whether a business fact is true.

Truth remains owned by:

- SQLite domain records;
- evidence artifacts;
- source identities;
- manifests/hashes;
- server-side validators;
- explicit human decisions.

The Agent cannot convert an untrusted statement into trusted evidence by summarizing it.

## 3. Two registries, different layers

The current project already has an Adapter Registry for external implementation choice.

Next-gen should add a separate Agent Tool Registry.

```text
Agent Tool
   ↓
Domain Service
   ↓
Adapter Registry
   ↓
CDP / CLI / Android / Bailian / other external implementation
```

Example:

```text
shop.collect_evidence_sample     # Agent-visible business tool
   ↓
ShopCollectionService            # business invariants
   ↓
AndroidDeviceAdapter             # platform implementation
```

This prevents the Agent from directly controlling low-level adapters in ways that bypass business invariants.

## 4. Tool contract we need

Every Agent-visible tool should expose at least:

```text
name
version
description
input_schema
output_schema
availability()
read_only
destructive
external_side_effect
concurrency_safe
requires_business_gate
estimated_cost_class
timeout
retry_policy
idempotency_policy
permission_class
execute()
```

Optional later fields:

```text
deferred_schema
required_capabilities
evidence_inputs
evidence_outputs
human_display
```

The Tool Registry must be generated from trusted application configuration and services, never from arbitrary model text.

## 5. Tool availability is a truth boundary

Claude-style environment-aware enablement maps cleanly to our real failure cases.

Examples:

- no Bailian key -> analysis tool unavailable/unconfigured;
- Android disconnected -> physical shop tool unavailable/needs_human;
- CDP profile not authenticated -> account collection unavailable/needs_human;
- required Opportunity not approved -> downstream Product Definition transition unavailable;
- Finished Product absent -> content-production tools unavailable.

Do not expose an unavailable tool and hope the Agent reasons around it.

## 6. Permission is more than security

The XHS PermissionPolicy should combine security + business authority.

Canonical decisions:

```text
ALLOW
ALLOW_WITH_BUDGET
ASK
DENY
```

Examples:

### ALLOW

- read already trusted SQLite facts;
- read job/analysis/opportunity state;
- inspect artifact metadata within managed runtime paths.

### ALLOW_WITH_BUDGET

- a bounded read-only model analysis;
- bounded public evidence collection after all prerequisites are proven;
- non-destructive verification.

### ASK

- starting a new physical Android/browser operation;
- high-volume or expensive collection;
- approving/rejecting business candidates;
- Product Definition approval;
- phase transition;
- any action explicitly defined as human ownership.

### DENY

- auto publish/like/favorite/comment;
- bypass evidence eligibility;
- rewrite immutable audit evidence;
- self-approve Opportunity/Product Definition;
- write outside approved runtime/workspace roots;
- silently replay unsafe physical navigation after interruption.

Headless `ASK` becomes a durable human-action state. It never degrades to `ALLOW`.

## 7. Context Builder is mandatory

The current real 243k-token analysis proves we cannot continue with a “serialize everything” strategy as the system grows.

Context must be assembled by layer:

```text
1. Agent role + current goal
2. Relevant business rules only
3. Current Project/Job/AgentRun state
4. Compact evidence facts required for this decision
5. Available Tool definitions only
6. Recent operational history / checkpoint summary
```

### Evidence layering

```text
Raw Evidence Layer
- immutable / authoritative
- complete artifacts and hashes
- never summarized away

Context Projection Layer
- compact structured facts
- every fact carries evidence/source references
- replaceable/recomputable
- only for reasoning efficiency
```

Before an authoritative write, the existing service/validator must re-check current trusted evidence rather than trusting the compact projection.

## 8. Context-control order

Recommended cheap-first approach:

1. do not load irrelevant tools;
2. do not load irrelevant business rules;
3. bound each tool result;
4. spill large results to managed storage and return structured references;
5. remove superseded operational reads/results;
6. reuse deterministic structured summaries;
7. only then use model-based summarization if still necessary;
8. keep recent step tail and current goal intact.

Measure every step. The `243,276`-token real analysis is the first fixed benchmark.

## 9. Agent state and durable business state must be separate

Proposed hierarchy:

```text
Project                 # complete business journey
  └─ Job                # durable unit of domain work
      └─ AgentRun       # one model-driven execution attempt/continuation
          └─ AgentStep  # model/tool/permission/checkpoint/etc.
```

`Job` retains existing durable semantics.

`AgentRun` adds:

- agent type/goal;
- model/prompt/runtime version;
- state;
- budgets;
- usage;
- checkpoint;
- parent/resume correlation.

`AgentStep` adds:

- step index/type;
- validated input/output;
- tool/model identity;
- permission decision;
- evidence refs;
- timing/usage;
- error category;
- idempotency key where applicable.

Do **not** persist hidden chain-of-thought.

## 10. Resume semantics

A checkpoint means “these durable facts are committed,” not “repeat the next external action automatically.”

For safe/read-only steps:

```text
crash -> explicit resume -> continue after last committed checkpoint
```

For unsafe physical/browser steps:

```text
crash/interruption
  -> Job needs_human
  -> operator inspects/re-establishes environment
  -> explicit new/resume action
  -> prerequisites/idempotency checked
  -> continue
```

Never replay Android/browser interactions solely from conversational history.

## 11. Specialized agents: later and narrow

Agent specialization is useful only when it reduces context or capability risk.

Possible later roles:

- ResearchAgent — read-heavy; may collect within explicit bounds; cannot approve business gates.
- AnalysisAgent — structured analysis; no physical navigation; cannot approve own Opportunity.
- VerificationAgent — read-only trust/audit checks; fail closed.

V1 should not include a free-form swarm, agent-to-agent social layer or recursive delegation.

## 12. Hooks/events

Cross-cutting runtime behavior should use explicit lifecycle events rather than service-specific ad hoc logging.

Minimum events:

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
run_succeeded
run_failed
run_cancelled
```

These should feed both durable telemetry and the UI timeline.

## 13. MCP position

MCP can be an optional extension path later for external tools/resources, but all MCP tools still pass through:

```text
availability
schema validation
permission
budget
result bounds
```

MCP resources are external inputs, not trusted XHS evidence unless a domain ingestion/validation path explicitly turns them into evidence.

## 14. Final design lesson

We should reproduce the **engineering discipline** of Claude Code, not Claude Code itself:

> Let the model choose among bounded next actions; let deterministic services decide what is valid; persist every operational boundary; require humans at real authority boundaries; keep context deliberately small and reconstructable.