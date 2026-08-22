# 10 — Open-Source Candidates and Fit Scorecard

- Evaluation date: 2026-08-23
- Purpose: choose components against the requirements in `06_AGENT_RUNTIME_REQUIREMENTS.md`, `07_WORKBENCH_REQUIREMENTS.md`, and `08_BACKEND_REQUIREMENTS.md`
- Status: Phase 3 recommendation; implementation still requires a small isolated spike before final production adoption

## 1. Executive result

The best architecture is **not**:

```text
Claude Code source
+ one giant open-source workbench
+ another generic backend
```

That would replace proven domain correctness with three large integration problems.

The strongest current composition is:

```text
Runtime core:
  Pydantic AI + selected Pydantic AI Harness capabilities

Agent ↔ UI interaction contract:
  AG-UI

React agent interaction / HITL primitives:
  CopilotKit (selective use)

Business workbench shell + domain pages:
  our own XHS shell, evolved from current React app
  with UI/design ideas selectively reused from licensed projects

Authoritative backend:
  existing FastAPI + SQLite + hardened domain services

Durability:
  existing JobService
  + new AgentRun / AgentStep / PermissionDecision / HumanAction / Checkpoint persistence

Not in first slice:
  LangGraph / Temporal / Microsoft Agent Framework distributed durability
  OpenHands Agent Server
  multi-agent swarm
```

This composition minimizes rewrite risk while adding the missing agent/workbench layer.

---

# 2. Agent Runtime candidates

Scoring uses 100 points. The exact number is a decision aid, not an objective benchmark.

Weights emphasize our actual constraints: license, Python/Pydantic fit, typed tools, HITL/permissions, context management, durability, observability, provider independence, local simplicity and ability to wrap existing domain services.

| Candidate | License / status | Fit | Main reason |
|---|---|---:|---|
| **Pydantic AI + Pydantic AI Harness** | MIT, current | **93/100** | Best Python/Pydantic fit; typed tools, deferred approval, hooks, usage limits, context compaction, sub-agent budgets; can sit above our services without replacing them. |
| **LangGraph** | MIT, current | **79/100** | Excellent durable checkpoint/HITL graph model; stronger than needed for first slice and would add graph semantics before we prove a simple Agent loop. |
| **Microsoft Agent Framework** | MIT, current | **76/100** | Production-oriented checkpointing, HITL, middleware, observability; broad/enterprise surface creates more integration/operational weight for a single-user Windows-local V1. |
| **OpenHands Software Agent SDK** | MIT, current | **68/100** | Mature conversation/event/confirmation model, but primarily a coding-agent runtime with workspace/sandbox assumptions not central to XHS. |
| **Langflow** | MIT, current | **55/100** | Strong visual agent/workflow builder and API/MCP exposure, but solves authoring/deployment of flows rather than our evidence-backed business-runtime problem. |
| **AutoGen** | MIT code, maintenance mode | **42/100** | Useful historical multi-agent architecture; upstream explicitly recommends Microsoft Agent Framework for new projects. |
| **Reconstructed Claude Code 2.1.88 source** | no open-source license found; proprietary restored source | **NOT ELIGIBLE** | Architecture study only; cannot be our implementation base. |

## 2.1 Pydantic AI + Harness — primary runtime candidate

Repositories:

- https://github.com/pydantic/pydantic-ai
- https://github.com/pydantic/pydantic-ai-harness

Why it leads:

- Python-native and Pydantic-native;
- typed/validated tools and outputs;
- `ApprovalRequired` / deferred-tool model maps naturally to `needs_human`;
- lifecycle hooks around model, tool validation and execution;
- usage/request limits;
- Harness context controls: tool-result clearing, deduplication, summarization, tiered/fallback compaction, context usage reporting;
- bounded sub-agent controls exist but are optional;
- no requirement to adopt a new database/server;
- can use our domain services as tools rather than replace them.

What still must remain ours:

- business `PermissionPolicy`;
- evidence trust and revalidation;
- AgentRun/AgentStep persistence;
- unsafe Android/browser restart rules;
- Product Definition gate;
- Bailian-only production routing unless explicitly changed.

### Recommendation

**PRIMARY — build the first isolated runtime spike on this stack.**

## 2.2 LangGraph — durability/workflow reserve

Repository: https://github.com/langchain-ai/langgraph

Strengths:

- first-class checkpoints;
- thread/checkpoint identity;
- pending writes;
- HITL/durable execution;
- explicit graph structure.

Why not first:

- our current workflow already has durable Jobs;
- a graph engine does not solve XHS evidence invariants;
- we first need to prove the simpler `AgentRun -> Tool -> Checkpoint` loop;
- physical action replay still requires our special fail-closed semantics.

### Recommendation

**RESERVE.** Re-evaluate if Agent workflows become sufficiently branched/concurrent that our own bounded loop + Jobs becomes awkward.

## 2.3 Microsoft Agent Framework — enterprise reserve

Repository: https://github.com/microsoft/agent-framework

Strengths:

- production-oriented agent/workflow framework;
- graph orchestration;
- checkpointing/HITL/time travel;
- middleware;
- OpenTelemetry;
- provider flexibility.

Why not first:

- broader multi-language/enterprise scope than our local single-user requirement;
- we would still need custom XHS evidence/business rules;
- adds another large abstraction surface while current backend already works.

### Recommendation

**RESERVE / REFERENCE.** Strong future option if operational requirements grow.

## 2.4 OpenHands SDK — not the right runtime base

Repositories:

- https://github.com/OpenHands/software-agent-sdk
- https://github.com/OpenHands/OpenHands

Strong ideas:

- durable conversation state;
- event log;
- waiting-for-confirmation state;
- max iterations/stuck detection;
- usage stats;
- Agent Server separation.

Mismatch:

- code/files/terminal/workspace/sandbox are primary assumptions;
- XHS Agent should operate through domain services, not a coding workspace;
- adopting its server would duplicate our FastAPI/business backend.

### Recommendation

**STUDY/SELECTIVE UI REUSE, not runtime base.**

## 2.5 Langflow — not an operator runtime

Repository: https://github.com/langflow-ai/langflow

It is excellent at visually authoring and deploying AI workflows, exposing them as APIs/MCP tools. But our user is not trying to graphically author arbitrary LLM workflows; the product needs a stable, evidence-backed business process with human gates.

### Recommendation

**REJECT AS CORE.** Keep as UI/workflow-builder reference only.

## 2.6 AutoGen

Repository: https://github.com/microsoft/autogen

Current repository explicitly says maintenance mode and recommends Microsoft Agent Framework for new projects.

### Recommendation

**REJECT FOR NEW CORE.**

---

# 3. Workbench / UI candidates

Our frontend requirements are unusual: we need real domain pages plus Agent run/timeline/HITL primitives. A complete coding-agent desktop app is therefore not automatically a better fit than a smaller UI protocol/library.

| Candidate | License | Fit | Main reason |
|---|---|---:|---|
| **AG-UI + CopilotKit + our domain shell** | MIT | **92/100** | Backend-independent event/HITL layer, first-party Pydantic AI integration, React components, lets us preserve Radar/Evidence/Opportunity pages. |
| **OpenHands Agent Canvas (selective components/patterns)** | MIT | **82/100** | Strong full agent control-center patterns and library entrypoints, but heavily coding-agent oriented and expects Agent Server semantics. |
| **assistant-ui** | MIT | **78/100** | Excellent composable React chat/tool/HITL primitives with custom runtime adapters; lacks full Project/Run/Evidence business-workbench structure. |
| **AionUi** | Apache-2.0 | **69/100** | Excellent Windows/local Cowork product and multi-agent UX, but very large Electron product with file/code/model/provider/team/office features we do not need. |
| **Langflow UI** | MIT | **56/100** | Visual workflow authoring, not an operator Project/Run/Evidence/HumanAction console. |
| **Refly** | custom Apache-derived terms | **REJECT** | Frontend carries additional logo/commercial-use restrictions; poor fit for a clean long-term product dependency. |

## 3.1 AG-UI — recommended Agent/UI wire contract

Repository: https://github.com/ag-ui-protocol/ag-ui

License: MIT.

AG-UI is a lightweight event protocol specifically between agents and user-facing applications. It defines standard event types and can run over SSE, WebSockets or other transports.

Important verified fit:

- repository includes first-party Pydantic AI integration;
- Python and TypeScript examples exist;
- designed for state synchronization and HITL;
- protocol is separate from MCP: MCP gives tools to agents; AG-UI connects agents to users.

### Recommendation

**PRIMARY interaction protocol candidate.**

We should still keep durable backend facts as truth; AG-UI events are the interaction/streaming projection.

## 3.2 CopilotKit — recommended React agent interaction layer

Repository: https://github.com/CopilotKit/CopilotKit

License: MIT.

Relevant capabilities:

- React support;
- streaming agent UI;
- backend tool rendering;
- shared state;
- human-in-the-loop;
- generative/structured UI;
- AG-UI integration;
- backend/framework independence.

### Why it beats forking a giant workbench

We already possess XHS-specific pages. CopilotKit can supply the missing interactive Agent layer without forcing Radar/Opportunity/Evidence into another product's data model.

### Recommendation

**PRIMARY UI primitive candidate**, selectively integrated into our shell.

Do not let client-side shared state become business authority.

## 3.3 OpenHands Agent Canvas — strongest full shell reference

Repository: https://github.com/OpenHands/OpenHands

License: MIT.

Strengths:

- polished self-hosted agent control center;
- run/conversation UX;
- backend selection;
- files/browser/settings/automation surfaces;
- frontend/server separation;
- package exposes library entrypoints for several UI modules.

Mismatch:

- coding/developer operations dominate;
- designed around OpenHands Agent Server / ACP agents;
- many terminal/files/browser concepts are irrelevant to XHS;
- adapting the entire app would likely cost more than extending our shell with the right primitives.

### Recommendation

**SECONDARY / SELECTIVE REUSE.** Study layout, run timeline and control-center patterns; evaluate individual library components during the UI spike, not an immediate full fork.

## 3.4 assistant-ui — strong alternative for chat/tool surfaces

Repository: https://github.com/assistant-ui/assistant-ui

License: MIT.

Strengths:

- composable React primitives;
- streaming/retry/attachments/accessibility;
- tool/JSON rendering;
- inline human approvals;
- AG-UI adapter;
- custom data-stream backend support.

Limitation:

It is fundamentally a production chat UI library, not a Project/Stage/Run/Evidence workbench.

### Recommendation

**BACKUP UI PRIMITIVE.** If CopilotKit integration proves too intrusive, assistant-ui is a credible lower-level option.

## 3.5 AionUi — excellent product, poor foundation fit

Repository: https://github.com/iOfficeAI/AionUi

License: Apache-2.0.

Current product includes:

- Electron desktop app;
- Windows/macOS/Linux;
- built-in Agent;
- many external coding agents;
- multi-agent team mode;
- MCP management;
- file access;
- cron/remote access;
- dozens of model/provider integrations;
- Office/PPT/Word/Excel capabilities;
- its own SQLite/Express/Electron infrastructure.

This makes it impressive as a **UX/reference product**, but costly as a base for our XHS business application.

### Recommendation

**DO NOT FORK AS CORE.** Use its Cowork/session/permission UX as design reference if desired.

## 3.6 Refly — license rejection

Repository: https://github.com/refly-ai/refly

Its current license adds conditions on top of Apache 2.0, including corporate/organizational commercial licensing requirements and frontend logo/copyright restrictions.

### Recommendation

**REJECT before technical evaluation.**

---

# 4. Generic backend candidates

## Decision: do not add a second generic backend

The current project already has:

- FastAPI;
- SQLAlchemy/SQLite;
- durable Jobs;
- hardened artifacts/evidence;
- adapters;
- business services;
- real UAT history.

Replacing this with OpenHands Server, Langflow backend or another framework backend would discard the most valuable part of the project.

The missing backend components are specific and small enough to add:

```text
AgentRun
AgentStep
PermissionDecision
HumanAction
Checkpoint
RunEvent / streaming projection
ContextBuilder
ToolRegistry
PermissionPolicy
```

### Recommendation

**KEEP existing backend. Add libraries, not a second platform.**

---

# 5. Durability choice

## V1

Use:

```text
existing JobService
+ SQLite AgentRun/AgentStep/checkpoint persistence
+ explicit idempotency
+ existing needs_human semantics
```

## Do not add yet

- Temporal;
- LangGraph checkpointer as business truth;
- DBOS/Prefect/Restate;
- distributed queue;
- Microsoft durable hosting.

## Re-evaluation trigger

Adopt a stronger workflow engine only after a measured requirement appears, such as:

- multiple machines/users;
- complex parallel graphs;
- long distributed waits;
- high task throughput;
- current local Job/runtime implementation becoming demonstrably difficult to maintain.

---

# 6. Proposed selected stack for isolated spike

```text
Frontend
  Current React/Vite domain shell
  + AG-UI event contract
  + CopilotKit selective components/HITL

Backend
  Current FastAPI
  + Pydantic AI runtime core
  + selected Pydantic AI Harness capabilities
  + original XHS ToolRegistry/PermissionPolicy/ContextBuilder

Persistence
  Current SQLite/JobService
  + AgentRun/AgentStep/PermissionDecision/HumanAction/Checkpoint

Domain core
  Existing Radar/XHS/Shop/Analysis/Evidence/Opportunity services unchanged at first

External integrations
  Existing Adapter Registry
  Existing Bailian/Android/CDP/etc.
```

---

# 7. What the spike must prove before final adoption

### Runtime spike

With fake model + fake domain tools:

- structured tool call;
- permission allow/ask/deny;
- durable AgentRun/Step;
- checkpoint;
- crash/reload/resume;
- token/step/time budget;
- large tool output bound/compaction;
- no hidden chain-of-thought persistence.

### UI spike

- backend streams run/tool/permission events through AG-UI or thin adapter;
- React shows live Run timeline;
- one human approval pauses then resumes;
- refresh reconstructs from SQLite/backend, not client state;
- existing Radar/Opportunity page still works;
- no business rule is moved into CopilotKit client state.

### Context benchmark

- fixed replay using the historical two-account / 22-evidence case;
- compare against historical 243,276 prompt tokens;
- maintain equal-or-stronger evidence grounding and revalidation.

If these spikes fail, the selected components are replaceable because the domain backend and UI contract remain separate.

---

# 8. Phase 3 conclusion

The current best answer to “Claude Code + open-source workbench + open-source backend” is more precise:

> **Learn the harness architecture from Claude Code, implement it with licensed Pydantic AI/Harness, connect it to a React workbench through AG-UI/CopilotKit, and preserve our existing hardened FastAPI/SQLite business backend instead of replacing it.**

This achieves the original goal while avoiding the legal and engineering cost of merging three monoliths.