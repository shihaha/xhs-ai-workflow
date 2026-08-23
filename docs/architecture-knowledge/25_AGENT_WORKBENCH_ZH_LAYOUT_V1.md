# 25 — Agent Workbench Chinese-first Layout V1

- Date: 2026-08-23
- Status: Stage 5.5 implementation slice
- Parent: `24_REAL_AGENT_ORCHESTRATION_V1.md`

## Goal

Turn the Agent page from an engineering projection console into a workbench that a non-programmer can operate without understanding English runtime terminology.

This slice changes presentation only. It does not change Job, AgentRun, HumanAction, continuation, Evidence, AnalysisService, or physical-worker authority.

## Layout decision

The `/agent` page now uses a task-oriented three-column desktop layout:

```text
left                    center                         right
任务记录                Agent 执行区                  依据与交接
- 最近任务              - 启动基于证据的分析          - 可用证据
- 中文状态              - 当前 Agent 正在做什么       - ChatGPT 交接
- 最近更新时间          - 等待人工确认
```

On narrow/mobile screens the center execution area moves first, followed by the supporting rails. The page must not create horizontal scrolling at the supported mobile width.

The layout deliberately does **not** invent a Project navigation hierarchy yet. There is still no authoritative durable Project entity in this ancestry.

## Chinese-first rule

Primary operator-facing UI is Chinese by default:

- page and section headings;
- state/status labels;
- buttons and confirmations;
- empty states;
- normal errors;
- permission/continuation explanations;
- Agent step descriptions;
- Job and Run detail headings.

Backend enum values and technical names are mapped to readable labels, for example:

```text
needs_human           -> 需要人工处理
succeeded             -> 已完成
approval_required     -> 等待批准
analysis.run_grounded -> 基于证据执行分析
job.read              -> 读取任务状态
```

Unknown technical values are never silently reinterpreted. They may still appear inside explicit technical-detail disclosure.

## Technical detail separation

Job IDs, AgentRun IDs, HumanAction IDs, raw tool names and other audit/debug identities remain available, but they are secondary.

The main page puts Job/Run raw records inside:

`查看技术详情（任务 ID / AgentRun / 原始记录）`

HumanAction and Evidence technical identities use their own small disclosure controls.

This preserves inspectability without requiring the operator to understand those identities during normal use.

## Why CopilotKit is not added in this slice

Existing architecture still keeps:

- AG-UI as the preferred future Agent/UI event contract;
- selective CopilotKit as the preferred Agent interaction/HITL primitive candidate;
- assistant-ui as backup;
- OpenHands Agent Canvas/AionUi as UX references.

However, the current production workbench still consumes durable REST projections rather than a real AG-UI streaming contract. Adding CopilotKit now would mix two separate experiments:

1. operator layout/localization;
2. streaming Agent UI framework integration.

V1 therefore keeps the current React/Vite shell and implements the new layout with project-owned React/CSS. When the backend exposes the real AG-UI-compatible event stream, the center Agent execution/timeline area can be evaluated for replacement with selective CopilotKit primitives without changing Job authority.

## Acceptance

V1 must satisfy:

1. the primary `/agent` operator surface is understandable in Chinese;
2. desktop presents task / execution / evidence context without the old vertical engineering-console stack;
3. mobile collapses without horizontal overflow;
4. waiting HumanAction approval/deny remains two-step and backend-authoritative;
5. technical IDs remain available but secondary;
6. Job/Run detail pages are Chinese-first;
7. frontend focused and full regression suites pass;
8. production build passes;
9. real Chromium desktop/mobile smoke has no console errors or horizontal overflow;
10. no backend/domain authority code changes are required.

## Non-goals

This slice does not:

- add a Project entity;
- add AG-UI streaming;
- install CopilotKit/assistant-ui;
- change AnalysisService or Agent Runtime semantics;
- enable physical XHS/Android/browser collection;
- merge stacked PRs automatically.
