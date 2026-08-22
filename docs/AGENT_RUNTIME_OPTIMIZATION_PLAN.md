# Agent Runtime Optimization Plan

Status: proposal only; no Phase A business rule is changed by this document.
Base branch: `feature/system-v1`
Experiment branch: `feature/agent-runtime-v1`

## 1. Goal

Upgrade the existing Xiaohongshu workbench from a set of durable domain workflows into a reusable, observable Agent Runtime without replacing the current evidence, job, or business-rule foundations.

The design may study publicly visible architectural behavior and documentation around mature coding agents, including reconstructed Claude Code 2.1.88 material, but **must not copy or vendor recovered Anthropic source code**. Implementation in this repository must be original and must remain compatible with the repository's own business specification and acceptance rules.

## 2. What must remain authoritative

The following existing rules are not weakened by the Agent Runtime:

- SQLite remains the only business database.
- Existing `JobService` state transitions, leases, logs, artifacts, and recovery semantics remain the durable outer execution boundary.
- A crashed or abandoned `running` job becomes `needs_human`; the new runtime must not silently resume unsafe work after restart.
- Real evidence, artifact files, hashes, source identity, and persisted model output remain the source of truth.
- No Agent may invent success, evidence, metrics, URLs, selectors, products, opportunities, or completion state.
- Phase boundaries remain explicit. An Agent cannot automatically advance from Phase A to Phase B or from approved analysis to product/content execution without the existing user authorization gate.
- V1 still does not automatically publish, like, favorite, or comment on Xiaohongshu.

## 3. Why this is worth doing

The current repository already has a strong durable execution core, but orchestration is mostly domain-specific. As later phases add product research, product construction, content research, content generation, review, and export, the number of ad-hoc service-to-service flows will grow quickly.

A small Agent Runtime can provide one reusable execution model:

`goal -> context -> model decision -> tool call -> permission -> execution -> observation -> checkpoint -> next step -> finish / needs_human`

This should reduce duplicated orchestration code and make long tasks easier to inspect and resume.

A second important opportunity is context cost. Current real analysis history has included extremely large prompts. The runtime should make context construction explicit and measurable so later work can reduce redundant context while preserving complete evidence traceability.

## 4. Architecture to add

### 4.1 AgentRun as an orchestration layer over Job

Do not replace `JobService`.

Add a lightweight runtime model where every Agent run belongs to exactly one durable Job. The Job remains the externally visible lifecycle; the Agent run stores the internal reasoning/execution trace needed to understand how that job progressed.

Suggested durable records:

- `agent_runs`
  - `id`
  - `job_id`
  - `agent_type`
  - `goal`
  - `state`
  - `step_count`
  - `model`
  - `prompt_version`
  - `token_in`
  - `token_out`
  - `created_at`
  - `updated_at`
- `agent_steps`
  - `id`
  - `agent_run_id`
  - `step_index`
  - `kind` (`model`, `tool`, `permission`, `checkpoint`, `summary`)
  - `tool_name`
  - `input_json`
  - `output_json`
  - `evidence_refs_json`
  - `started_at`
  - `completed_at`
  - `status`
  - `error_category`

Do not persist hidden chain-of-thought. Persist only operational decisions, tool calls, structured outputs, summaries, evidence references, usage and errors.

### 4.2 Tool contract and Tool Registry

Introduce an original Python tool interface for workbench capabilities.

Each tool should expose at least:

- unique `name`
- human description
- Pydantic input schema
- Pydantic/structured output contract
- `read_only`
- `destructive`
- `concurrency_safe`
- permission requirement
- timeout/budget metadata
- idempotency behavior
- execution function

Initial tools should be wrappers around existing code, not rewrites of working adapters.

Candidate tools:

- `radar.list_candidates`
- `account.read_profile`
- `account.read_latest_notes`
- `shop.preflight`
- `shop.collect_evidence_sample`
- `analysis.shared_demand`
- `opportunity.get`
- `opportunity.request_review`
- later: product/content/media tools

The registry should support environment-aware enablement so a missing Android device, browser profile, API key, or model configuration makes the relevant tool unavailable rather than causing the Agent to hallucinate a path forward.

### 4.3 Permission Policy

Move execution permissions into one reusable policy layer instead of scattering all decisions through orchestration code.

Recommended categories:

- `allow`: deterministic reads from SQLite and existing artifacts.
- `allow_with_budget`: bounded model calls and bounded read-only collection when prerequisites are already satisfied.
- `ask`: device navigation that can change external state, expensive/high-volume collection, business-rule transitions, approvals, and phase changes.
- `deny`: automatic publish/like/favorite/comment; writes outside approved runtime/repository paths; attempts to bypass evidence/approval gates.

Every permission result should persist:

- requested tool
- decision (`allow`, `deny`, `ask`)
- rule/policy reason
- user approval reference when applicable

A headless/background execution mode must be fail-closed: if a tool requires `ask`, the run becomes `needs_human` instead of guessing.

### 4.4 Agent Loop

Implement a bounded loop rather than an open-ended autonomous process.

Pseudo-flow:

1. claim durable Job
2. build bounded context
3. ask model for one structured next action
4. validate action schema
5. resolve tool from registry
6. run permission policy
7. execute one tool
8. persist tool input/output/evidence refs
9. checkpoint progress
10. continue, finish, or enter `needs_human`

Required budgets:

- maximum steps
- maximum wall-clock time
- maximum model calls
- maximum input/output tokens
- per-tool timeout
- per-tool retry policy

Budget exhaustion must be an explicit durable outcome, not an infinite retry.

### 4.5 Context Builder

Build model context from explicit layers:

1. Agent role and current goal
2. immutable business constraints relevant to this job
3. current Job/AgentRun state
4. only the evidence facts required for the next decision
5. available tool schemas
6. prior compact operational summary

The builder should emit usage metrics so the UI can show where tokens are being spent.

For large evidence sets, use a two-level strategy:

- Level A: immutable raw evidence remains stored exactly as it is today.
- Level B: compact, structured evidence summaries may be generated for orchestration, but every field must retain source evidence IDs and must be reproducible/revalidated.

A summary is never allowed to upgrade evidence trust or replace the existing approval/trust validator.

### 4.6 Checkpoint and resume

After every successful tool call, persist enough state to restart from a known boundary.

Important compatibility rule: repository policy currently requires interrupted `running` jobs to become `needs_human`. Therefore restart behavior is:

`process crash -> Job becomes needs_human -> operator explicitly resumes -> AgentRun continues from last committed checkpoint`

Do not automatically replay physical Android/browser actions after restart.

Use idempotency keys for safe operations so a human-approved resume does not duplicate already committed work.

### 4.7 Hooks and observability

Add internal hooks/events around:

- `before_model`
- `after_model`
- `before_tool`
- `permission_decision`
- `after_tool`
- `checkpoint`
- `needs_human`
- `run_finished`

Initially hooks only produce structured logs/metrics. Do not build a plugin marketplace in V1.

The existing `/jobs` UI can later show an Agent timeline:

- current goal
- current stage
- step count
- tool calls
- permission decisions
- evidence produced/consumed
- token usage
- elapsed time
- last checkpoint
- reason for `needs_human`

## 5. Agent types to introduce first

Do not begin with a multi-agent swarm.

Start with three narrow roles:

### `ResearchAgent`

Purpose: collect/read already-authorized evidence and determine which evidence is still missing.

Rules:
- read-heavy
- cannot approve opportunities
- cannot enter a later phase
- cannot invent missing evidence

### `AnalysisAgent`

Purpose: prepare and execute bounded structured analysis using trusted evidence.

Rules:
- consumes evidence through the Context Builder
- output must pass existing Pydantic/evidence-grounding validators
- cannot modify evidence trust
- cannot approve its own opportunity

### `VerificationAgent`

Purpose: independently check that persisted job/artifact/hash/model facts match the runtime files and database before a stage is marked complete.

Rules:
- read-only by default
- discrepancies fail closed

A later Phase B/C can add Product and Content agents after the runtime is proven.

## 6. What to borrow as design ideas, not code

Useful mature-agent ideas worth reimplementing independently:

- one common tool contract
- explicit tool registry
- schema validation before execution
- read-only/destructive/concurrency metadata
- central permission decisions
- bounded tool-use loop
- per-step checkpoints
- explicit context builder
- context/token accounting
- compaction/summarization with source references
- separate specialized agents with limited tool sets
- structured hooks and execution timeline

Ideas not needed in the first workbench runtime:

- terminal UI cloning
- Git worktree isolation for every business task
- coding-specific file edit semantics
- swarm/team agents
- voice mode
- arbitrary shell access for the business Agent
- automatic background continuation after unsafe interruption

## 7. Implementation sequence

### Stage 0 - Regression lock

Before runtime code:

- run current focused backend tests
- run current frontend tests/build
- record baseline failures separately from new work
- add no new business rule

### Stage 1 - Runtime skeleton

Add:

- `backend/app/agent_runtime/types.py`
- `backend/app/agent_runtime/tools.py`
- `backend/app/agent_runtime/permissions.py`
- `backend/app/agent_runtime/context.py`
- `backend/app/agent_runtime/runtime.py`
- database records/migration path for `agent_runs` and `agent_steps`
- unit tests for registry, schema validation, permission policy and bounded loop

No existing domain workflow is switched over yet.

### Stage 2 - Wrap one safe workflow

Use a read/analysis-only path first, preferably a controlled analysis/verification job.

Acceptance:

- same business result as current service path
- same evidence references
- same fail-closed behavior
- additional Agent timeline is persisted
- no Phase A acceptance rule changes

### Stage 3 - Token/context optimization

Instrument prompt composition first; then reduce duplicated context.

Acceptance:

- exact evidence coverage contract still passes
- same or better structured output reliability
- lower prompt-token usage on a fixed replay fixture
- every compact fact remains traceable to evidence IDs

### Stage 4 - Human-gated collection orchestration

Wrap selected collection steps.

Acceptance:

- unsafe restart still becomes `needs_human`
- no automatic Android replay
- device/browser prerequisites are tool enablement facts
- pause/cancel/human takeover remain available

### Stage 5 - Workbench timeline UI

Extend `/jobs` rather than building a separate debug application.

## 8. Testing requirements

Minimum new test matrix:

- tool schema rejects malformed model action
- disabled tool cannot be selected/executed
- destructive/phase-changing tool cannot bypass permission policy
- `ask` in headless mode becomes `needs_human`
- max-step budget terminates safely
- max-token/model-call budget terminates safely
- tool timeout is durable and categorized
- checkpoint survives process reconstruction
- explicit resume continues after last committed step
- repeated resume does not duplicate idempotent tool output
- raw evidence/hash trust rules are unchanged
- compact context cannot create a new trusted fact
- Agent cannot approve its own opportunity
- Agent cannot enter Phase B without explicit authorization

## 9. Success criteria

The optimization is successful only if it gives the workbench these properties without weakening the current evidence model:

- long workflows are understandable as a step timeline
- tools have one consistent contract
- human permission points are centrally enforced
- tasks can resume from durable checkpoints after explicit intervention
- context/token cost is measurable and reducible
- later product/content phases can reuse the runtime instead of adding bespoke orchestration loops
- current Phase A facts, evidence, hashes, approvals and historical audit records remain unchanged

## 10. First coding target

The first implementation PR on `feature/agent-runtime-v1` should contain only:

1. runtime data types
2. Tool protocol + registry
3. PermissionPolicy
4. bounded Agent loop with a fake model and fake tools
5. persistence for AgentRun/AgentStep
6. tests

Do not connect Android, Qianfan, XHS browser automation, opportunity approval, or content production in the first coding slice.
