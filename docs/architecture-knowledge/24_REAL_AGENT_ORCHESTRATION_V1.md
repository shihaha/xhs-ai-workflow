# 24 — Real Agent Orchestration V1

Status: Stage 5 implementation slice stacked on Durable Agent Continuation Executor V1 / PR #20.

## Purpose

Start a brand-new bounded Agent orchestration from existing durable evidence, let the Agent decide whether an existing grounded analysis service should run, and preserve Job / AgentRun / HumanAction / Evidence authority throughout the sequence.

This is the first slice where the workbench can start new real Agent work. It does **not** add Project, generic Product Definition/Product Build/Content workflows, or direct Android/XHS/browser control.

The authority chain remains:

`operator -> Agent orchestration API -> JobService + durable AgentRun/dispatch -> bounded Agent Runtime -> existing domain services -> Evidence/Artifact/Job state`

## Initial launch transaction

`POST /api/v1/agent-runtime/jobs`

Input is intentionally narrow:

- one operator-authored goal;
- 1–20 canonical durable Evidence IDs already exposed by `AnalysisService.list_evidence()`.

The server owns the RunBudget. React cannot raise token/step/time limits.

`AgentInitialLaunchCoordinator` commits the following in one SQLite transaction:

1. a running `agent_orchestration` Job admitted through `JobService.create_running_in_session(...)`;
2. the initial running AgentRun;
3. an AgentRun->Job binding;
4. a seed checkpoint containing the exact Evidence refs and bounded safe metadata summaries;
5. an `agent_initial_dispatches` row in `queued` state;
6. an audit Job log.

The seed checkpoint solves the initial-context problem without weakening the model boundary. Production model execution still requires real durable Evidence refs. Raw evidence content, credentials, paths, cookies, provider prompts and physical worker state are not copied into the seed.

The generic `POST /api/v1/jobs` endpoint rejects `agent_orchestration`; new Agent Jobs must enter through this bounded launch path.

## Initial executor and crash safety

`AgentInitialExecutor` is owned by the FastAPI lifespan.

A fresh `queued` dispatch with `attempts=0` may be claimed and driven through `JobBoundAgentRuntime.run_bound(run_id)`.

Initial execution is deliberately **not replayable** after an uncertain process loss. If a dispatch had already entered `running` (`attempts>0`) before restart, the executor does not call the model or Tool loop again. It invokes the canonical interrupted-run recovery and projects a durable `needs_human` boundary instead.

This differs from approved continuation recovery: continuation has one exact durable approved Tool identity and the existing continuation Runtime knows how to reuse an already-committed result without replay. An initial run has no equivalent exact action identity, so automatic replay would be unsafe.

If automatic model configuration is absent and there is no recoverable durable dispatch, the initial executor does not start an idle polling thread.

## Production Agent surface

V1 continues to expose only:

- `job.read`
- `analysis.run_grounded`

`analysis.run_grounded` is the existing adapter over `AnalysisService.create()`. The Agent does not implement analysis logic itself and cannot directly persist a trusted Opportunity outside the existing service rules.

The tool still requires explicit HumanAction approval under the existing permission policy. Therefore the normal flow is:

`initial Agent model -> proposes analysis.run_grounded -> AgentRun/Job needs_human -> operator approve -> durable continuation -> AnalysisService -> Agent continues/finishes`

Existing AnalysisService evidence validation, deep-verification gates, persistence and Opportunity semantics remain authoritative.

## Evidence-scope ceiling

The initial selected Evidence set is a durable evidence ceiling, not merely UI guidance.

Before `AgentContinuationExecutor` approves an `analysis.run_grounded` HumanAction, it validates the tool arguments against the configured runtime and then proves that every requested Evidence ID already exists in the durable Job history.

If the model proposes an unknown/out-of-scope Evidence ID:

- approval fails before the Job is reclaimed;
- HumanAction remains pending;
- source AgentRun remains `needs_human`;
- Job remains lease-free `needs_human`;
- AnalysisService is not invoked.

Subsequent trusted domain Tool results may add new durable evidence refs to the same Job history. Those refs can become available to later bounded analysis because they were produced by an existing deterministic service, not invented by the model.

The production Agent instruction also states this rule, but the backend check is authoritative.

## Workbench UI

The Agent workbench loads safe `AnalysisEvidence` metadata only when backend capabilities say `start_grounded_orchestration=true`.

The start panel lets the operator:

1. write a bounded goal;
2. select up to 20 existing Evidence items;
3. review a second confirmation step;
4. create the real Agent Job.

React never sets Job/AgentRun state optimistically. After the command it reloads durable projections.

The start capability is disabled when automatic Agent model configuration is unavailable.

## Explicit non-goals

This slice does not:

- create a durable Project entity;
- let the Agent bypass AnalysisService or write trusted Opportunity directly;
- expose Android/XHS/browser/Qianfan/shop physical workers as synchronous Agent tools;
- add Phase B Product Definition;
- add Phase C Product Build;
- add Phase D Content orchestration;
- merge stacked PRs automatically.

## Verification requirements

Dedicated verification must cover:

- atomic JobService-owned initial Job + AgentRun + seed + dispatch creation;
- initial executor success from real durable seed refs;
- permission wait before `analysis.run_grounded` executes;
- evidence-scope expansion rejection before reclaim;
- started-initial-dispatch restart fail-closed with zero model replay;
- real API start and durable Workbench reconstruction;
- unknown Evidence rejection;
- generic Jobs API Agent creation bypass rejection;
- FastAPI lifespan ownership of initial + continuation executors;
- direct AnalysisService/Agent Tool parity regression;
- prior continuation and Workbench contracts;
- complete Agent Runtime acceptance;
- focused/full frontend tests and production build;
- compile/release-boundary/workflow checks.

Repository-wide `Agent Runtime Spike Verification` remains the broad baseline-vs-spike regression gate. No PR is merged automatically.
