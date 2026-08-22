# Claude Code 2.1.88 Architecture Study

- Inspected: 2026-08-23
- Primary study repository: https://github.com/claude-code-internals/claude-code-runnable
- Source status: reconstructed from the published `@anthropic-ai/claude-code@2.1.88` package/source map
- License status at inspection: **no root `LICENSE` file found**
- Reuse policy for this project: **architecture study only; do not copy, vendor, or derive implementation text/source from the reconstructed Anthropic code**

## Why this source is useful

The reconstructed repository is unusually useful for understanding how a mature coding agent is organized end to end: model loop, tool contracts, permissions, context management, sub-agents, MCP, task lifecycle, caching and observability. It is not a safe implementation dependency for `xhs-ai-workflow` because the restored code originates from proprietary Anthropic software and the inspected repository does not provide an open-source license for that source.

Our goal is therefore to extract **design patterns**, then implement our own domain-specific runtime independently.

## 1. Core conversation/tool loop

The mature loop is conceptually:

```text
user / task input
  -> normalize messages
  -> construct bounded system + project context
  -> call model (streaming)
  -> parse structured tool calls
  -> validate tool input
  -> permission decision
  -> hooks / execution pipeline
  -> execute tool
  -> normalize result
  -> append tool result to conversation
  -> repeat until final output or a stop condition
```

Important engineering details:

- tool-use and tool-result pairing is normalized before provider calls;
- usage/tokens are accounted for as part of the loop;
- tool execution is not a direct `model -> function` jump: validation, permission and lifecycle hooks sit in between;
- context compaction can be triggered during long sessions;
- final response and tool calls are two outcomes of the same run loop.

### Relevance to XHS

This is the missing orchestration layer in the current workbench. We do **not** need its terminal UI or coding-specific semantics. We need the same separation between:

```text
model decision
  -> validated action
  -> permission policy
  -> bounded domain tool
  -> durable result
```

## 2. Tool contract

Claude Code's tool architecture demonstrates why a common tool abstraction needs more than a function name and JSON schema. Mature tool metadata covers concepts equivalent to:

- stable name and description;
- detailed usage prompt/help;
- typed input and output schemas;
- environment-aware availability;
- deferred/lazy schema exposure;
- read-only vs state-changing behavior;
- destructive behavior;
- concurrency safety;
- permission check;
- validation;
- execution;
- bounded result mapping;
- user-facing presentation.

### Relevance to XHS

Our existing Adapter Registry already proves the value of capability-based indirection. A new **Agent Tool Registry** should sit one level above domain services/adapters and expose only business-safe operations such as:

```text
radar.list_candidates
account.read_profile
account.read_latest_notes
shop.preflight
shop.collect_evidence_sample
analysis.shared_demand
opportunity.read
opportunity.request_review
```

The Agent should not receive raw arbitrary browser/ADB/SQL/filesystem access merely because those mechanisms exist below the service layer.

## 3. Permission model

Claude Code separates permission policy from tool implementation. Its observed model includes structured decisions equivalent to:

```text
allow
ask
deny
passthrough
```

with decision provenance/reason. It also distinguishes permission modes. A critical mature behavior is that a non-interactive `dontAsk` style mode is fail-closed for operations without prior authorization; it is not equivalent to “allow everything.”

Permission checks are layered:

1. global policy/mode;
2. tool-specific policy;
3. deeper operation-specific validation for dangerous capabilities.

### Relevance to XHS

For our business system, permission is not only a security concern. It also encodes **business authority**.

Examples:

- read trusted SQLite facts: allow;
- bounded model analysis with configured budget: allow-with-budget;
- new Android navigation, expensive collection, phase transition or product-definition approval: ask;
- auto publish/like/favorite/comment or bypassing an evidence gate: deny.

A headless `ask` must become a durable human-action state such as `needs_human`; it must never silently become allow.

## 4. Context construction and compaction

Claude Code treats context as an explicit subsystem rather than concatenating every available fact into every request. Observed concepts include:

- layered project/user/system memory;
- Context Builder;
- tool-schema search/deferred loading;
- prompt/schema/file caches;
- automatic compaction;
- session-memory compaction;
- small/cheap tool-result compaction;
- manual/partial compact;
- session/history resume;
- context usage analysis.

### Relevance to XHS

This is directly relevant to the real Phase A analysis that consumed **243,276 input prompt tokens for two accounts / 22 eligible evidence facts**.

Our next runtime must separate:

```text
Level A: immutable authoritative raw evidence
Level B: compact orchestration facts that cite Level A evidence IDs
```

Compaction may reduce model context but may never:

- replace the authoritative evidence chain;
- upgrade evidence trust;
- invent facts;
- bypass current evidence revalidation before a successful business write.

## 5. Sub-agent model

Claude Code demonstrates specialized agents with independent context, tool sets and lifecycles. The architecture supports concepts such as:

- read-only exploration/planning agents;
- foreground/background execution;
- independent/forked context;
- per-agent model selection;
- worktree/workspace isolation;
- inter-agent messaging;
- task lifecycle and usage reporting.

### Relevance to XHS

The important idea is **capability isolation**, not “more agents = better.”

Initial XHS architecture should remain single-agent first. Later roles can be narrow:

- `ResearchAgent`: read-heavy evidence gathering and completeness checks;
- `AnalysisAgent`: structured evidence-grounded analysis;
- `VerificationAgent`: independent, read-only trust/consistency validation.

No first-version swarm, autonomous agent teams, or nested delegation is required.

## 6. MCP as an extension bus

Claude Code treats MCP as an external capability bus with:

- server discovery and connection lifecycle;
- explicit connection states;
- dynamically registered tools;
- schemas supplied by the server;
- resource listing/reading;
- deferred/lazy tool exposure;
- normal permission handling around MCP tools.

### Relevance to XHS

MCP can become a later integration boundary for optional external capabilities, but it must not become the source of truth for domain state. XHS business truth remains in our validated SQLite/evidence layer.

## 7. Observability and lifecycle

Mature agent runtime concerns include:

- task/run state;
- current tool execution;
- usage and cost;
- background tasks;
- hooks around model/tool execution;
- session history;
- structured failures;
- context usage;
- restart/resume behavior.

### Relevance to XHS

The current `/jobs` page already has part of this foundation. The missing layer is a model-driven Agent timeline under or alongside a Job:

```text
Job
  -> AgentRun
      -> AgentStep: model
      -> AgentStep: permission
      -> AgentStep: tool
      -> AgentStep: checkpoint
      -> AgentStep: summary
```

Do not persist hidden chain-of-thought. Persist operational facts only: structured actions, tool inputs/outputs, decisions, evidence references, usage, errors and compact summaries.

## 8. What we should borrow conceptually

- explicit bounded agent loop;
- rich common Tool contract;
- environment-aware tool availability;
- centralized permission policy with provenance;
- lazy/deferred tool schemas;
- Context Builder and measured compaction;
- bounded specialized agents;
- lifecycle hooks;
- task/run usage and event timeline;
- MCP as an optional extension bus.

## 9. What we should not copy

- reconstructed Anthropic source code;
- terminal/Ink UI architecture;
- coding-specific file-edit rules;
- arbitrary shell access;
- Git worktree behavior as a universal business-task primitive;
- swarm/team agents in V1;
- automatic background continuation after unsafe physical interruption;
- Claude-specific provider assumptions.

## 10. Bottom line

Claude Code is valuable as a **design reference for an agent harness**, not as a codebase to merge into the XHS product.

The reusable lesson is the layered architecture:

```text
context + model
      -> structured action
      -> tool validation
      -> permission
      -> bounded execution
      -> durable event/result
      -> next model turn
```

Our implementation must preserve the stronger business/evidence rules already proven in `xhs-ai-workflow` while independently adopting this runtime shape.