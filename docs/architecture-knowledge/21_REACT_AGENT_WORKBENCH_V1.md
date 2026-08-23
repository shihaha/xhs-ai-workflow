# React Agent Workbench V1

## Status

This document records the first React projection layer built on top of the already validated durable Agent Workbench read APIs.

It does not redefine Agent Runtime authority, Job semantics, continuation, manual ChatGPT handoff, B/C workflows, or physical worker ownership.

## Base

The UI slice is stacked on PR #17 / `spike/workbench-chatgpt-handoff-read-v1` at the validated head that exposes:

- Agent Jobs;
- Agent Runs;
- Human Actions;
- Job Evidence refs;
- Job Artifact summaries;
- durable ChatGPT handoff task projections.

The frontend consumes these backend projections as facts. It does not reconstruct lifecycle authority from UI state.

## Routes

### `/agent`

Read-only Agent Workbench overview:

- durable Agent Job summaries;
- bound AgentRun summaries;
- HumanAction lifecycle summaries;
- ChatGPT handoff task lifecycle summaries.

ChatGPT handoffs explicitly surface backend-projected:

- `needs_chatgpt` as “需要 ChatGPT 处理”;
- `result_ready` as “已交回 / result ready”;
- `is_current_binding`;
- `authority_ambiguous`.

The UI never turns these labels into permission to mutate a Job.

### `/agent/jobs/{job_id}`

Read-only durable Job detail:

- Job lifecycle/state/stage;
- current AgentRun identity;
- Run history;
- pending HumanActions;
- ordered Evidence refs;
- redacted Artifact summaries.

Artifact local paths and arbitrary metadata remain absent because the backend projection does not expose them.

### `/agent/runs/{run_id}`

Read-only AgentRun detail:

- Run lifecycle summary;
- durable step timeline;
- tool identity and tool-call identity where projected;
- step/run Evidence refs;
- HumanAction summaries;
- projected error category/detail.

Raw AgentStep input/output and provider prompt/context remain absent.

## Explicit non-goals

V1 does not add:

- a Project entity or Project page;
- approval/deny/retry/resume/cancel controls;
- a continuation driver;
- direct Android/XHS/browser worker control;
- raw handoff task/result display;
- raw Job input;
- local Artifact paths;
- provider prompts, credentials, cookies, or browser/device state.

There is still no authoritative durable Project entity in this ancestry. The UI must not synthesize one from Job input, folder names, or frontend state.

## Authority boundary

The existing authority chain remains:

`Agent -> durable Job / JobService -> physical worker -> Evidence / Artifact / Job state`

`needs_chatgpt` and `result_ready` are backend-derived durable projections. In particular, `result_ready` does not authorize the React client to set a Job running or create a continuation AgentRun.

## Verification

The dedicated `React Agent Workbench V1 Verification` workflow is expected to run:

- focused Agent Workbench React tests;
- complete frontend regression;
- production frontend build;
- existing Workbench read/projection/handoff contract tests;
- complete Agent Runtime acceptance;
- compile and release-boundary checks.

The repository-wide `Agent Runtime Spike Verification` remains required as the broad regression gate before any merge decision.
