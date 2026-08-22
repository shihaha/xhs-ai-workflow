# ADR-003: Use AG-UI as the Agent/UI interaction candidate and preserve our domain workbench shell

- Status: proposed (approved for isolated UI spike; production adoption depends on spike acceptance)
- Date: 2026-08-23

## Context

The next workbench needs streaming AgentRun/tool/permission events, human-in-the-loop interactions and reconnectable state while preserving XHS-specific Radar, Evidence and Opportunity pages.

Full agent desktop/workbench products such as AionUi and OpenHands Agent Canvas are powerful but carry substantial coding-agent/workspace/backend assumptions. Replacing our UI wholesale would force domain screens and backend truth into another product model.

AG-UI is an MIT event protocol dedicated to agent/user interaction and has verified Pydantic AI integration. CopilotKit is an MIT React interaction layer built around AG-UI and supports streaming, tool rendering, shared-state UI and human-in-the-loop flows.

## Decision

For the first UI spike:

- keep/evolve the current React/Vite XHS workbench shell;
- expose a backend run/event contract compatible with or adapted to AG-UI;
- evaluate selective CopilotKit primitives for Agent interaction and HITL;
- keep Project/Stage/Run/Evidence business views as our own domain UI;
- use OpenHands Agent Canvas and AionUi as licensed UX/component references, not monolithic foundations.

Client state remains a projection. Backend SQLite/domain records remain authoritative.

## Alternatives considered

- full OpenHands Agent Canvas fork — strong control-center UX, but too coding-agent/Agent-Server centric.
- full AionUi fork — excellent Windows Cowork UI, but very large Electron product with many irrelevant providers/agents/office/file capabilities.
- assistant-ui — strong lower-level chat/HITL alternative; retained as backup if CopilotKit is too intrusive.
- Langflow frontend — workflow authoring rather than business operator workbench.
- Refly — rejected due additional license/commercial/frontend restrictions.

## Consequences

Positive:

- preserves existing domain screens;
- agent runtime remains replaceable behind a stable UI protocol;
- avoids importing a large unrelated desktop/server stack;
- HITL and streaming do not need to be invented from scratch.

Costs/risks:

- we still build Project/Run/Evidence workbench layout ourselves;
- AG-UI events need a durable-backend adapter; live events cannot be treated as truth;
- CopilotKit shared state must not be used for business authority;
- a spike must verify React/Vite integration without forcing a Next.js rewrite.

## Evidence / references

- `docs/source-research/OPENHANDS_AGENT_CANVAS_STUDY.md`
- `docs/architecture-knowledge/07_WORKBENCH_REQUIREMENTS.md`
- `docs/architecture-knowledge/10_OPEN_SOURCE_CANDIDATES.md`
- https://github.com/ag-ui-protocol/ag-ui
- https://github.com/CopilotKit/CopilotKit
- verified AG-UI Pydantic AI integration under `integrations/pydantic-ai/`