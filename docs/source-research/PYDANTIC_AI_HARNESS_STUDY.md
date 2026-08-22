# Pydantic AI + Pydantic AI Harness Study

- Inspected: 2026-08-23
- Core: https://github.com/pydantic/pydantic-ai
- Harness: https://github.com/pydantic/pydantic-ai-harness
- License: **MIT** for both inspected repositories
- Reuse status: technically/license-compatible candidate components; final dependency decision deferred until open-source selection phase

## Why this is unusually relevant to our project

Unlike reconstructed Claude Code source, Pydantic AI and its Harness are current, openly licensed Python projects. They implement many of the same mature agent-harness ideas in a form that is naturally compatible with our Python/FastAPI/Pydantic backend.

The key lesson is not “replace our backend with Pydantic AI.” It is that modern agent behavior can be composed from small capabilities around a typed core loop rather than built as one giant agent class.

## 1. Typed agent loop

Pydantic AI provides:

- typed tool schemas derived from Python signatures;
- validated tool arguments;
- typed/validated model output;
- provider-independent model routing;
- explicit run/message history;
- streaming and lifecycle events;
- usage accounting.

This is a strong match for our existing Pydantic-heavy domain model and strict structured-analysis contracts.

## 2. Capability composition

Pydantic AI Harness treats agent features as composable capabilities. Its current components include concepts such as:

- filesystem/workspace;
- shell restrictions;
- repo context;
- planning;
- sub-agents;
- context compaction;
- tool-output limits;
- memory;
- skills;
- MCP;
- guardrails;
- spend limits;
- tool approval;
- durable execution.

A complete `Coder` agent is itself just a composition of capabilities rather than a second framework hidden inside the framework.

### Relevance to XHS

This strongly supports building our runtime as:

```text
Agent Runtime Core
  + Domain Tools
  + Permission Capability
  + Context Capability
  + Evidence/grounding hooks
  + Usage/Budget capability
  + Persistence/Event capability
```

instead of making `AnalysisService` or a future `MainAgent` own every concern.

## 3. Human-in-the-loop / deferred tools

Pydantic AI has a particularly relevant deferred-tool model.

A tool may:

- always require approval;
- conditionally require approval based on validated arguments/context;
- be executed externally;
- pause the current run and return structured pending requests;
- later continue from prior message history plus structured approval/external results.

The follow-up can use a new run identity while preserving conversation correlation.

### Relevance to XHS

This maps closely to our current rule:

```text
unsafe/approval-required step
  -> durable needs_human
  -> operator decision/action
  -> explicit resume/new run
```

The model should never keep a process alive while waiting indefinitely for a person.

A crucial security lesson from Pydantic AI documentation is also applicable: **human tool approval is not itself an authorization boundary**. Server-side code must still validate whether a business operation is authorized, even if a client claims it was approved.

For our system this means:

- Phase gates remain server-side business invariants;
- evidence eligibility is revalidated server-side;
- a UI approval payload cannot manufacture authority by replaying conversation history;
- durable approval records should bind actor/decision/target/version/time.

## 4. Context management

Pydantic AI Harness provides several concrete context strategies:

- `ClampOversizedMessages`;
- sliding-window trimming;
- clearing old tool results;
- deduplicating repeated file reads;
- model summarization;
- tiered cheap-first compaction;
- fallback compaction;
- near-limit warnings;
- live context-usage reporting.

Important design qualities:

- preserve tool-call/tool-result pairing;
- deterministic/cheap reductions run before LLM summarization;
- context limits can be expressed relative to a model's actual window;
- provider-reported usage is used as an accounting anchor when possible;
- oversized tool returns are treated as a first-class source of context bloat;
- usage can be reported to a UI.

### Relevance to XHS

The real `243,276`-token Phase A request should become a regression benchmark.

Our likely policy:

1. do not send raw files/images/large JSON when a verified compact projection will do;
2. keep immutable raw evidence separately;
3. use evidence-ID-linked compact facts;
4. trim old operational tool results before summarizing important state;
5. bound every individual tool result;
6. display context usage and model token usage in the workbench;
7. re-resolve authoritative evidence before a business success commit.

## 5. Sub-agent control

Pydantic AI Harness sub-agents demonstrate good defaults for safe specialization:

- child has its own message history;
- task must be self-contained;
- tools do not need to be inherited by default;
- shared capabilities can be explicit;
- per-delegate token/request budgets;
- wall-clock timeout;
- max delegation count;
- controlled failure containment;
- model choice can be restricted to a predefined menu;
- child events can be streamed.

### Relevance to XHS

If we later add specialized agents, each should have:

- explicit allowed Tool set;
- explicit business stage;
- max calls/steps/model budget;
- timeout;
- no implicit destructive-tool inheritance;
- events/usage visible in the parent run;
- inability to self-authorize a human business decision.

This argues against starting with an unconstrained swarm.

## 6. Hooks and middleware-like lifecycle

Pydantic AI exposes lifecycle interception around:

- run start/end/error;
- graph/node execution;
- model requests;
- tool validation;
- tool execution;
- output validation/processing;
- tool preparation;
- deferred calls;
- event streams.

### Relevance to XHS

Cross-cutting rules such as logging, usage measurement, evidence-reference collection, permission auditing and checkpoint creation should live in explicit lifecycle hooks/middleware rather than being copied into every domain service.

Likely internal events:

```text
before_run
after_run
before_model
after_model
before_tool_validate
after_tool_validate
permission_decision
before_tool
after_tool
checkpoint
needs_human
run_finished
run_failed
```

## 7. Durable execution

Pydantic AI can attach external durability providers such as Temporal, DBOS, Prefect or Restate, preserving progress across failures/restarts and supporting long waits/HITL.

### Relevance to XHS

This is useful as a **separation-of-concerns reference**, but it does not prove we should add a workflow server now.

Our existing `JobService` already provides valuable single-machine durable semantics in SQLite. The first next-gen runtime should likely build on that foundation and prove the new AgentRun/AgentStep model before adding infrastructure such as Temporal.

If later requirements exceed SQLite/local execution, a durability provider can be reevaluated.

## 8. Directly attractive pieces vs concepts only

### Strong candidates for direct evaluation later

- Pydantic AI typed Agent/Tool core;
- approval/deferred-tool APIs;
- usage limits/accounting;
- hooks/capabilities;
- selected context-management utilities;
- MCP capability where needed.

### Concepts to implement in our domain regardless of framework

- server-side business PermissionPolicy;
- evidence-linked Context Builder;
- AgentRun/AgentStep persistence in our business DB;
- durable human decisions;
- XHS-specific Tool metadata and prerequisites;
- unsafe physical-action resume rules.

## 9. Risks / unresolved questions

- current library surface is evolving quickly; version pinning and upgrade discipline would be required;
- using Harness components should not force coding-agent assumptions into the XHS business domain;
- provider-native behaviors may differ from Bailian/our configured models;
- framework message history cannot become the source of truth for evidence/business authority;
- external durability systems would add operational complexity beyond current single-user needs.

## 10. Bottom line

Pydantic AI + Harness is currently one of the strongest **licensed architecture matches** found for the runtime layer because it gives us Claude-Code-like harness concepts in Python without forcing us to copy proprietary source.

It should be evaluated in Phase 3 as a runtime/component candidate, not automatically adopted before our requirements matrix is complete.