# 11 — Architecture Decisions

- Date: 2026-08-23
- Status: research architecture selected; framework/UI production adoption remains conditional on isolated spikes

## 1. Selected target shape

```text
┌──────────────────────────────────────────────────────────────┐
│ XHS Workbench UI                                             │
│                                                              │
│ Project / Stage / Runs / Human Actions / Evidence            │
│ Radar / Accounts / Opportunities / future B-C-D domain views │
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
│ future Product Definition / Product Build handoff            │
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

### C. Product Definition is a separate human gate

`approved Opportunity` means product research may continue. It does not authorize build.

Required chain:

```text
approved Opportunity
 -> Product Research
 -> Product Definition candidate
 -> human Product Definition approval
 -> Product Build
 -> Finished Product + human UAT
 -> Content
```

ADR: `ADR-005-product-definition-gate.md`.

### D. Claude Code is a design reference, not a dependency

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

## 5. Why we are not using a full open-source workbench unchanged

Our product has domain-specific screens and facts that generic Agent workbenches do not understand:

- ranked demand candidates;
- account/profile/note/shop evidence;
- evidence eligibility and lineage;
- Opportunity review;
- future Product Definition;
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
| Product Definition approval | future domain human-review record |
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
- later draft Product Definitions/content within the correct stage.

It may not:

- manufacture trusted evidence;
- override phase gates;
- approve its own business decisions;
- replay unsafe physical actions blindly;
- publish/like/favorite/comment automatically;
- mark success because a model said so.

## 9. Final architecture statement

The next XHS system should become **agentic around a deterministic evidence-backed core**, not replace that core with an autonomous agent platform.

That is the central decision all implementation work must preserve.