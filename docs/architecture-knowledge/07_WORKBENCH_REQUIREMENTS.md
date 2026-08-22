# 07 — Workbench Requirements

- Version: research baseline v1
- Date: 2026-08-23
- Status: requirements for open-source UI/workbench evaluation; not final UI selection

## 1. Workbench mission

The next workbench is not a collection of pretty dashboard cards. It is the operator surface for a real A → B → C → D business system.

It must let a user answer at any moment:

```text
What project am I working on?
What business stage is it in?
What is currently running?
What did the Agent actually do?
What evidence/results exist?
Why did something stop?
What requires my decision?
What is allowed to happen next?
What did it cost in time/tokens?
Can I safely resume/retry/cancel?
```

## 2. The UI must not own business truth

The workbench is a projection/control surface over backend authority.

Frontend must not determine:

- Job success;
- evidence eligibility;
- Opportunity evidence level;
- phase transition authorization;
- Product Definition approval;
- whether a physical action is safe to resume;
- whether a content package is publishable;
- model/provider/tool availability.

It renders backend facts and submits explicit commands/decisions.

## 3. Top-level information architecture

Recommended primary hierarchy:

```text
Workspace
  └─ Project
      ├─ Stage / Business Journey
      ├─ Runs & Jobs
      ├─ Human Actions
      ├─ Evidence / Deliverables
      ├─ Domain Views
      └─ Activity / Audit Timeline
```

For V1 a single workspace/user is sufficient, but the structure should not force all business work into one flat global Jobs page.

## 4. Project page

A Project represents one business direction/product journey, not one Agent conversation.

Minimum Project overview:

- project name/id;
- current business stage A/B/C/D;
- stage status;
- current authorized transition;
- latest durable result;
- active/pending Jobs and AgentRuns;
- unresolved human actions;
- key linked Opportunity/Product Definition/Finished Product records;
- key evidence/deliverables;
- next allowed actions;
- recent timeline.

### Critical distinction to display

The UI must visibly separate:

```text
IMPLEMENTED CAPABILITY
PROVEN CAPABILITY
CURRENTLY AUTHORIZED BUSINESS STAGE
```

Example: content code may exist while content production is not yet authorized for a project.

## 5. Business-stage journey

The workbench should make the intended journey obvious:

```text
A Demand Radar
  -> human Opportunity decision
B Product Research / Product Definition
  -> human Product Definition approval
C Product Build
  -> human UAT / Finished Product
D Content Research & Production
  -> human review / external publishing flow
```

Each stage should show:

- entry prerequisites;
- current state;
- completed outputs;
- blockers;
- human gate if any;
- next legal transition.

The UI must never imply a later stage is complete because code for that stage exists.

## 6. Run center

The generic workbench needs a run-centric view above the current domain Jobs list.

### Run list columns/filters

At minimum:

- Project;
- Job type;
- Agent type;
- state;
- current step/stage;
- started/updated time;
- duration;
- model;
- token usage;
- human-action indicator;
- error category;
- evidence/artifact count where meaningful.

Filters:

```text
active
waiting human
failed
succeeded
cancelled
by project
by stage
by agent/tool type
```

## 7. AgentRun detail / timeline

This is one of the most important missing primitives in the current UI.

Minimum timeline event types:

```text
run started
model request/result summary
tool requested
tool validation
permission decision
tool started/result
checkpoint committed
human action requested
resume
run succeeded/failed/cancelled/budget exhausted/stuck
```

Each timeline row should show:

- time;
- event/step type;
- short human-readable summary;
- status;
- duration;
- model/tool identity;
- token usage for model steps;
- linked evidence/artifacts;
- error/permission reason where applicable.

Expandable details may show structured input/output, but secrets and private model chain-of-thought must not be rendered.

## 8. Human Action Inbox

Human approval must be a first-class UI object, not hidden inside logs.

Inbox should show unresolved actions such as:

- re-login/captcha/device reconnect;
- selector/layout investigation;
- approve/reject Opportunity;
- approve Product Definition;
- approve a high-cost collection;
- inspect an unsafe interrupted physical job;
- content/UAT review.

Every action should show:

- what is being requested;
- why;
- which Project/Run/Step requested it;
- evidence/preview required for the decision;
- exact scope of approval;
- what will become allowed if approved;
- options such as approve/reject/resume/cancel as defined by backend policy.

The UI must not manufacture an approval record locally. Backend response is authoritative.

## 9. Evidence lineage view

Because evidence integrity is a core product differentiator, the workbench needs a reusable evidence inspector.

It should support tracing:

```text
claim / opportunity / decision
  -> evidence ID
  -> domain record
  -> job/artifact
  -> source identity / URL
  -> manifest / SHA / current verification state
```

Minimum presentation:

- evidence type;
- source/account/product/note identity;
- producing Job;
- artifact path shown safely/relatively;
- hash/manifest status;
- eligibility/trust status;
- historical vs current indicator;
- linked consumers (analysis/opportunity/etc.).

This is more valuable to our business than a generic code terminal panel.

## 10. Context / usage observability

For Agent runs show:

- input/output tokens;
- context window usage if known;
- prompt/tool-schema size estimates where available;
- model/provider;
- model calls;
- tool calls;
- retries;
- wall time;
- cost if reliably known.

The UI should make regressions such as a 243k-token two-account analysis visible rather than hiding them in provider logs.

## 11. Capability status

The workbench needs a factual environment/capability page derived from backend health.

Statuses should distinguish:

```text
configured
ready
unconfigured
unavailable
needs_login
needs_human
unsupported_current_layout
rate_limited / provider_failure
```

Relevant capabilities include:

- Bailian text/vision/image;
- Qianfan browser profile;
- XHS CDP/CLI reads;
- Android device;
- runtime storage/SQLite;
- future MCP/external tools.

An unavailable capability should explain which operations are blocked.

## 12. Resume / retry / cancel UX

Controls must correspond to real backend commands, not optimistic UI state.

### Resume

Show:

- last checkpoint;
- why the run stopped;
- whether resume is safe;
- what prerequisite/human action is required;
- whether continuation creates a new run/attempt.

### Retry

A retry should clearly indicate whether it is:

- a new analysis using same evidence;
- a safe idempotent tool retry;
- a new physical collection attempt;
- a regenerated content revision.

Do not use one generic Retry button for semantically different operations.

### Cancel

Cancel should show backend-confirmed final state and retained evidence/artifacts.

## 13. Domain screens remain important

A generic Agent shell must not erase useful current business views.

We still need domain pages such as:

- Radar / ranked candidates;
- account profile/note/shop evidence;
- Opportunity review;
- Product Research/Definition when implemented;
- Product Build handoff/status;
- Content Studio;
- System status.

The ideal shell lets domain pages coexist with generic Project/Run/HumanAction/Evidence primitives.

## 14. Frontend architecture requirements

Preferred qualities for an open-source shell/candidate:

- React/TypeScript friendly or realistically embeddable;
- clean API/event boundary from backend;
- reusable conversation/run/timeline components;
- routable domain pages;
- component/library entrypoints preferred;
- no hard dependency on a particular cloud backend;
- self-host/local support;
- Windows browser use works normally;
- testable with mocked backend events;
- accessible state/status controls;
- mature layout/navigation/state management;
- license permits modification/commercial/private use.

## 15. Streaming/event transport

The frontend should be able to receive incremental backend events using one of:

```text
SSE
WebSocket
bounded polling fallback
```

The transport must support reconnect/re-read from durable state. Live streaming is a convenience; persisted state is truth.

If the connection drops, the UI should reload the current run/timeline from backend rather than assume the last streamed event was final.

## 16. State management

Frontend state should separate:

```text
server-authoritative query state
local UI state
transient streaming projection
```

Do not duplicate business state into a client-only store that can diverge from SQLite/backend.

## 17. Search and navigation

The user should be able to move from a high-level Project into exact evidence without knowing IDs manually.

Recommended navigation:

```text
Project
 -> Opportunity
 -> supporting account
 -> shop/note evidence
 -> producing Job
 -> AgentRun step
 -> artifact
```

and reverse navigation from a failed step back to the affected Project/business stage.

## 18. Error presentation

Errors should be categorized, not shown as generic red banners.

Examples:

```text
device_disconnected
selector_changed
needs_login
model_timeout
model_retry_exhausted
evidence_grounding_failed
evidence_changed_after_model
worker_restart_required
budget_exhausted
permission_denied
human_action_required
```

UI should distinguish:

- failure;
- waiting for a person;
- blocked by configuration;
- safely incomplete;
- historical/non-blocking warning.

## 19. No fake dashboard rule

Every progress percentage, status count, evidence badge, model usage number and next action must come from backend facts or be explicitly labeled as an estimate.

No placeholder/demo business data in production runtime.

## 20. Workbench candidate scorecard for Phase 3

Every open-source UI/workbench candidate should be scored on:

1. license clarity;
2. maintained/current status;
3. React/TS integration fit;
4. generic run/timeline primitives;
5. human approval UX;
6. event streaming/reconnect;
7. local/self-host support;
8. ability to embed/extend domain pages;
9. backend independence;
10. testability;
11. security/workspace assumptions;
12. removal cost if abandoned later;
13. Windows usability;
14. UI quality;
15. amount of irrelevant coding-agent baggage.

A beautiful UI that requires rewriting our domain truth into its internal backend should score poorly.

## 21. Minimum V1 workbench surface

The first credible next-gen shell does not need every future screen. Minimum:

```text
Project Overview
Runs
Run Timeline
Human Action Inbox
Evidence links
System/Capability Status
existing Radar + Opportunity domain views
```

Once those are stable, Product Definition and Content stages can be added without redesigning the shell.

## 22. Acceptance criteria

A UI/workbench candidate or implementation passes only if:

- refreshing the page reconstructs state from backend;
- dropped stream connection does not create false completion;
- pending human action is clearly visible;
- permission denial reason is visible;
- Job vs AgentRun vs AgentStep can be distinguished;
- evidence can be traced from a business result;
- token/time usage is readable;
- Project business stage is explicit;
- code-exists/proven/authorized are not conflated;
- the UI cannot bypass a backend business gate;
- current domain pages can be integrated without forking the shell beyond maintainability.

These requirements drive Phase 3 workbench selection.