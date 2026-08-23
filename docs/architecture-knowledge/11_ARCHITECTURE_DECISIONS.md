# 11 — Architecture Decisions

- Date: 2026-08-23
- Status: research architecture selected; framework/UI production adoption remains conditional on isolated spikes

## 1. Selected target shape

```text
┌──────────────────────────────────────────────────────────────┐
│ XHS Workbench UI                                             │
│                                                              │
│ Project / Stage / Runs / Human Actions / Evidence            │
│ Radar / Accounts / Opportunities / B-C handoffs / Content   │
│ + selective CopilotKit agent interaction components          │
└───────────────────────┬──────────────────────────────────────┘
                        │ AG-UI-compatible events/commands
                        ▼
┌──────────────────────────────────────────────────────────────┐
│ Existing FastAPI Workbench API                               │
│ + Project/Run/HumanAction/Event endpoints                    │
└───────────────────────┬──────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────┐
│ Agent Runtime                                                │
│                                                              │
│ Pydantic AI candidate core                                   │
│ ContextBuilder                                               │
│ ToolRegistry                                                 │
│ PermissionPolicy                                             │
│ Budget / Hooks / Usage / Checkpoint                          │
│ AgentRun / AgentStep persistence                             │
└───────────────────────┬──────────────────────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────┐
│ Existing hardened Domain Services                            │
│ Radar / XHS / Shops / Analysis / Opportunity / Content       │
│ + thin Product Definition / Finished Product handoffs        │
└───────────────────────┬──────────────────────────────────────┘
                        ▼
┌───────────────────────────────┬──────────────────────────────┐
│ Adapter Registry              │ SQLite + managed artifacts   │
│ Qianfan/CDP/CLI/Android/model │ evidence/manifest/SHA        │
└───────────────────────────────┴──────────────────────────────┘
```

## 2. Decisions that are accepted now

### A. Existing domain backend remains authoritative

Do not replace the FastAPI/SQLite/Job/Evidence/Adapter/business-service core with a generic third-party agent backend.

Reason: this is the part with the most real UAT and domain hardening.

ADR: `ADR-001-preserve-domain-core.md`.

### B. Local durability first

Keep JobService + SQLite as V1 durability. Add AgentRun/AgentStep/checkpoint records rather than introducing a distributed workflow server.

ADR: `ADR-004-local-durability-first.md`.

### C. Product Definition remains a separate human gate

`approved Opportunity` does not authorize product build.

The authority chain remains:

```text
approved Opportunity
 -> external/product-specific B work
 -> human-approved Product Definition handoff
 -> external/product-specific C work
 -> Finished Product + human UAT
 -> Content
```

ADR: `ADR-005-product-definition-gate.md`.

### D. B/C are not systemized yet

Phase B (product research/definition work) and Phase C (product creation/build work) vary too much by product type to justify a generic workflow today.

Current implementation must therefore preserve only thin durable handoff records around B/C. Do not build a generic B Agent, C Agent, B/C state machine, universal product builder or speculative product workflow until enough diverse completed products exist to extract recurring patterns from evidence.

ADR: `ADR-006-defer-bc-systemization.md`.

### E. Claude Code is a design reference, not a dependency

Use its mature architecture concepts — Tool contract, permission, context, sub-agent isolation, hooks, usage — but independently implement domain-specific behavior with licensed components.

## 3. Decisions approved for isolated spike, not yet final production adoption

### A. Pydantic AI + selected Harness capabilities

Why:

- highest runtime fit score;
- MIT;
- Python/Pydantic-native;
- typed tools/output;
- deferred human approval;
- lifecycle hooks;
- context/usage controls;
- optional sub-agents without requiring them.

ADR: `ADR-002-agent-runtime-spike.md`.

**Production acceptance condition:** pass the runtime spike and existing domain regression baseline.

### B. AG-UI + selective CopilotKit

Why:

- MIT;
- separates Agent/User interaction from runtime;
- verified Pydantic AI integration;
- React/HITL/streaming primitives;
- preserves our XHS domain pages rather than forcing an external product model.

ADR: `ADR-003-agent-ui-contract.md`.

**Production acceptance condition:** prove React/Vite integration, backend reconstruction after refresh and zero client-side business-authority leakage.

## 4. Explicitly not selected for V1 core

### Reconstructed Claude Code 2.1.88

Not license-eligible. Study only.

### AionUi as full fork

Great Cowork product and UX reference; too much Electron/provider/file/team/office/agent infrastructure unrelated to XHS.

### OpenHands Agent Canvas as full fork

Strong agent control center and useful component/design reference; too coding-agent/Agent-Server centric to justify replacing our shell/backend.

### LangGraph as initial runtime

Excellent durability/checkpoint option; reserve until a measured graph/concurrency requirement appears.

### Microsoft Agent Framework as initial runtime

Strong production framework; currently broader/heavier than needed.

### AutoGen

Current upstream maintenance mode; not a new-project core.

### Langflow

Visual workflow builder, not our business operator workbench/runtime.

### Refly

Rejected on license/commercial/frontend restrictions before technical adoption.

### Generic B/C workflow engine

Rejected for the current phase because the recurring B/C process has not yet been proven across sufficiently varied finished products.

## 5. Why we are not using a full open-source workbench unchanged

Our product has domain-specific screens and facts that generic Agent workbenches do not understand:

- ranked demand candidates;
- account/profile/note/shop evidence;
- evidence eligibility and lineage;
- Opportunity review;
- Product Definition and Finished Product handoffs;
- Finished Product/UAT;
- content research/production gates.

A full fork would require either:

1. stuffing these facts into a foreign conversation/task model, or
2. maintaining two parallel navigation/state systems.

Both are worse than keeping our shell and adding open-source Agent interaction primitives.

Therefore the selected definition of “use an open-source workbench” is:

> reuse mature open-source **workbench primitives/protocols/components**, while the XHS business shell remains ours.

This is a smaller and more maintainable integration boundary.

## 6. Stable interfaces we own regardless of framework

To avoid framework lock-in, our code should own these abstractions:

```text
AgentRuntime
AgentAction
DomainTool
ToolRegistry
PermissionPolicy
ContextBuilder
AgentRunRepository
CheckpointRepository
HumanActionService
RunEventPublisher
```

Pydantic AI or another framework may implement internals behind these boundaries.

CopilotKit/AG-UI may render/transport events, but backend events and commands remain ours.

## 7. Source-of-truth matrix

| Fact | Authority |
|---|---|
| XHS/Qianfan/shop raw business fact | domain DB + validated evidence/artifact |
| Evidence trust/eligibility | backend validators/services |
| Job lifecycle | JobService / SQLite |
| Agent operational lifecycle | AgentRun/AgentStep persistence |
| Permission decision | backend PermissionPolicy + persisted human authority |
| Opportunity review | domain human-review record |
| Product Definition approval | human-approved handoff record |
| Finished Product / UAT | accepted product handoff / dossier |
| UI streaming state | projection only |
| compact Agent context | non-authoritative projection |
| model conversation history | reasoning input/history, never business truth |

## 8. V1 role of AI

The Agent gets more autonomy over **sequence**, not over **truth or authority**.

It may:

- inspect available facts;
- choose an allowed next tool;
- ask for missing evidence;
- run bounded analysis;
- summarize operational status;
- prepare a human decision;
- orchestrate A-side research/analysis and later D-side content work within the correct gates.

It may not:

- manufacture trusted evidence;
- override phase gates;
- approve its own business decisions;
- replay unsafe physical actions blindly;
- publish/like/favorite/comment automatically;
- mark success because a model said so;
- invent a generic B/C process simply because the A/B/C/D business diagram contains those labels.

## 9. B/C handoff principle

The workbench should be capable of representing:

```text
Opportunity
  ↓
Product Definition handoff
  ↓
Finished Product handoff + UAT
  ↓
Content
```

But the internal work between Opportunity → Product Definition and Product Definition → Finished Product remains product-specific and external to the generalized workbench workflow for now.

This keeps the business gates explicit without pretending the product-making process is already standardized.

## 10. Final architecture statement

The next XHS system should become **agentic around a deterministic evidence-backed core**, not replace that core with an autonomous agent platform.

It should automate what has been proven repeatable, preserve explicit handoffs for what is still product-specific, and only extract new workflows after repeated real cases demonstrate a stable pattern.

That is the central decision all implementation work must preserve.