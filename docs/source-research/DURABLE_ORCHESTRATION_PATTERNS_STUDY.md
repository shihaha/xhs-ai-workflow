# Durable Orchestration Patterns — LangGraph, Microsoft Agent Framework, AutoGen

- Inspected: 2026-08-23
- LangGraph: https://github.com/langchain-ai/langgraph — MIT
- Microsoft Agent Framework: https://github.com/microsoft/agent-framework — MIT
- AutoGen: https://github.com/microsoft/autogen — code under MIT; current repository explicitly states maintenance mode
- Reuse status: architecture/reference study; direct dependency decisions deferred to Phase 3

## Purpose

This study checks whether the runtime requirements inferred from Claude Code and Pydantic AI are isolated ideas or broader production-agent patterns.

The answer is clear: modern systems converge on several foundations — durable state/checkpoints, human interruption, bounded execution, events/observability and explicit workflow/runtime separation.

## 1. LangGraph: checkpointed state as a primitive

LangGraph describes itself as a low-level orchestration framework for long-running, stateful agents. Its current public architecture emphasizes:

- durable execution;
- human-in-the-loop interrupts;
- memory/state;
- graph-based orchestration;
- observability.

Its checkpoint package explicitly models:

- a checkpoint as a snapshot of graph state;
- a `thread_id` as a durable series of checkpoints;
- optional `checkpoint_id` to resume from a particular point;
- pending writes so successful sibling work does not necessarily have to rerun after another node fails.

### Relevance to XHS

Checkpointing should be a **first-class runtime concept**, not a log line.

However, XHS has a stronger physical-world safety constraint than a generic graph:

- Android/browser navigation may have external side effects or an unknowable external state;
- an interrupted physical action must not be automatically replayed merely because a graph engine knows the previous node;
- current `running -> needs_human` behavior after unsafe interruption must remain authoritative;
- explicit operator resume plus idempotency/prerequisite revalidation is required.

So generic durable replay semantics must be narrowed by our domain policy.

## 2. Microsoft Agent Framework: production workflow convergence

The current Microsoft Agent Framework positions itself around production-grade agents and workflows with:

- Python/.NET/Go implementations;
- provider flexibility;
- graph workflows;
- sequential/concurrent/handoff/group patterns;
- checkpointing;
- streaming;
- human-in-the-loop;
- time-travel/debugging;
- OpenTelemetry observability;
- middleware;
- declarative agents/skills.

### Relevance to XHS

This reinforces three decisions:

1. **agent loop and workflow orchestration are related but different layers**;
2. **checkpoint/HITL/telemetry are core runtime requirements**, not optional UI polish;
3. multi-agent orchestration is available later but is not necessary to prove our first runtime.

The framework is broad and enterprise-oriented. For a single-user Windows-local V1 it may be more infrastructure than we need; Phase 3 should evaluate integration cost rather than adopt it because it is feature-rich.

## 3. AutoGen: useful history, poor new-project default

The current AutoGen repository states that AutoGen is in maintenance mode and directs new users to Microsoft Agent Framework. AutoGen remains useful for understanding layered/event-driven and multi-agent ideas, but it should not be a preferred new runtime base for this project.

Its Studio is also explicitly described as a rapid-prototyping UI rather than a production-ready end-user application.

### Relevance to XHS

Do not select a framework solely because it has a visual multi-agent studio. Maintenance status, production boundary and migration path matter more than screenshots.

## 4. Cross-framework convergence

Across Claude Code, Pydantic AI/Harness, LangGraph, OpenHands and Microsoft Agent Framework, the recurring patterns are:

```text
Typed / validated actions
Tool or capability registry
Human approval / interruption
Durable run state
Checkpoint or persisted history
Execution budgets / iteration limits
Usage + observability
Context management
Specialized agents only when useful
External capability protocols (MCP etc.)
UI/runtime separation
```

This convergence is strong enough to treat these as requirements for our next workbench architecture rather than as optional features copied from one vendor.

## 5. What remains domain-specific to XHS

Generic agent frameworks do **not** automatically solve our hardest correctness rules:

- authoritative evidence identity and SHA/manifest validation;
- current-evidence revalidation before Opportunity approval;
- original-score candidate ordering;
- exact shop sample semantics;
- `preflight` vs Opportunity-eligible evidence distinction;
- physical device restart safety;
- Opportunity approval vs Product Definition approval;
- no Phase B/C/D transition without explicit human authority;
- no automatic publish/like/favorite/comment.

These must remain in our domain services/policies even if a framework executes the outer Agent loop.

## 6. Bottom line

The next architecture should not choose between “deterministic workflow” and “Agent.” It needs both:

```text
Agent chooses among allowed next actions
      -> deterministic domain tool enforces business contract
      -> durable runtime records step/checkpoint
      -> human gate interrupts where required
```

A framework is valuable only if it strengthens this structure without replacing our proven evidence and business rules.