# OpenHands Agent Canvas + Software Agent SDK Study

- Inspected: 2026-08-23
- UI/control-center repository: https://github.com/OpenHands/OpenHands
- Agent runtime/server repository: https://github.com/OpenHands/software-agent-sdk
- License: **MIT** for both inspected repositories
- Reuse status: strong workbench-shell/server-interface reference; direct reuse to be evaluated in Phase 3

## Why this matters to the XHS workbench

OpenHands is especially relevant because its current architecture separates a **workbench/control-center UI** from the **Agent Server/runtime**. That is much closer to what we need than trying to turn our current React pages into an all-knowing business state machine.

The key architectural lesson is:

```text
UI / Workbench
      -> stable server API + event stream
      -> agent/runtime/backend
      -> workspace/tools/domain systems
```

The UI renders and controls execution, but it does not execute agent actions or own the authoritative runtime state.

## 1. Agent Canvas boundaries

The current OpenHands Agent Canvas is responsible for concepts such as:

- conversations/runs;
- browser/files/terminal views;
- settings and backend selection;
- automation UI;
- translating user actions into Agent Server API calls;
- consuming backend state/events;
- packaging UI as standalone and embeddable/library entrypoints.

It is explicitly not responsible for:

- directly executing agent actions;
- providing the sandbox itself;
- owning credentials outside the backend;
- running scheduled automations without a separate automation backend.

### Relevance to XHS

Our next frontend should similarly be a **projection/control surface** over authoritative backend facts.

It should not decide:

- whether evidence is trusted;
- whether a phase transition is authorized;
- whether an Opportunity is eligible;
- whether a physical action is safe to resume;
- whether a Product Definition is approved.

Those remain backend rules.

## 2. Replaceable agent backends

Agent Canvas can connect to multiple agent backends and supports third-party agents through an agent protocol. This proves the value of designing a workbench UI around a stable run/event contract rather than embedding one agent implementation into every screen.

### Relevance to XHS

Our frontend should ideally depend on a contract like:

```text
Project
Run
RunEvent / Step
HumanAction
Artifact / EvidenceRef
CapabilityStatus
```

not on private fields from one Pydantic/LangGraph/OpenHands agent implementation.

That gives us room to change the runtime later without rewriting the workbench UI.

## 3. Agent Server / Conversation state

The OpenHands Software Agent SDK persists a rich Conversation state. The inspected model includes concepts equivalent to:

- idle;
- running;
- paused;
- waiting for confirmation;
- finished;
- error;
- stuck;
- deleting;
- persisted agent/workspace config;
- max iterations;
- stuck detection;
- confirmation/security policy;
- event log;
- usage statistics;
- persistent agent-specific state;
- hooks;
- secret registry;
- branching/forked event history.

### Relevance to XHS

Several of these should exist in our AgentRun layer even though our outer `Job` states remain authoritative for business work:

- explicit run state;
- max steps;
- stuck/budget detection;
- append-only operational event timeline;
- usage statistics;
- durable checkpoint/current step;
- human confirmation state;
- secret-aware event redaction.

## 4. Event log rather than only final status

OpenHands uses an event-oriented conversation history, which makes it possible to inspect what happened, rebuild a view, stream progress, and support branching/forking.

### Relevance to XHS

The current `/jobs` page has logs and artifacts, but an Agent runtime needs a more structured operational trace.

Recommended shape:

```text
AgentRun
  Event: run_started
  Event: model_requested
  Event: model_completed
  Event: tool_requested
  Event: permission_decided
  Event: tool_started
  Event: tool_completed
  Event: checkpoint_committed
  Event: human_action_required
  Event: run_finished
```

`AgentStep` can be the durable structured unit, while an event stream can project those steps to the UI.

We should not persist private model chain-of-thought; the event log should contain only operational facts required for observability/recovery/audit.

## 5. Confirmation policy

OpenHands has confirmation policy abstractions including always/never/risk-threshold confirmation.

### Relevance to XHS

This is a useful pattern but our business requires a richer policy than generic security risk. Our PermissionPolicy must include:

- tool identity;
- tool metadata;
- current business stage;
- evidence prerequisites;
- environment/device readiness;
- estimated budget/cost;
- side-effect class;
- explicit business approvals.

So OpenHands confirmation is a reference, not a drop-in replacement for business authorization.

## 6. Sandbox/workspace separation

OpenHands makes workspace/sandbox execution an explicit layer and warns that running directly on a host grants filesystem access.

### Relevance to XHS

Our business system does not need arbitrary coding-agent host access. A future Agent runtime should default to **domain tools**, not shell/filesystem.

When product-building/Codex work eventually exists, it should be a separately scoped development workspace, not a reason to give the XHS research Agent unrestricted machine access.

## 7. What appears directly reusable conceptually

- workbench UI separated from runtime;
- stable server/event API;
- run/conversation state model;
- append-only event/timeline approach;
- waiting-for-confirmation state;
- usage metrics;
- stuck/iteration limits;
- replaceable agent backend idea;
- UI components designed around run/conversation lifecycle.

## 8. What should not be imported blindly

- terminal/browser/files developer tooling that is irrelevant to XHS business workflows;
- unrestricted workspace access;
- coding-agent assumptions about task completion;
- cloud/multi-backend complexity in V1;
- an external Conversation history as a replacement for our immutable evidence/business database.

## 9. Bottom line

OpenHands is currently one of the strongest references for the **workbench boundary**:

> frontend renders and controls; backend/runtime executes; domain state remains authoritative.

Phase 3 should evaluate whether Agent Canvas components/library entrypoints can accelerate our UI, but selection must be based on integration cost and whether domain pages can coexist cleanly with its run-centric shell.