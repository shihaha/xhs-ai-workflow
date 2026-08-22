# 04 — Current System Problems and Architectural Gaps

- Audit date: 2026-08-23
- Goal: identify what a next-generation workbench should improve **without discarding already-proven correctness**.

## Priority model

- `P0` — business/architecture ambiguity that could make the new system do the wrong thing.
- `P1` — major engineering limit affecting scale, reliability, observability or development speed.
- `P2` — important cleanup or future-design issue, but not a reason to stop current Phase A.

## P0 — Opportunity approval and Product Definition are not the same gate

The current handoff defines:

```text
approved Opportunity
→ independent product research
→ human-approved concrete Product Definition
→ product build
```

However, the repository already exposes a Product API whose Product record can be created from an approved Opportunity and whose core fields are much thinner than a complete Product Definition.

That creates a semantic danger for future orchestration: an Agent could interpret `Opportunity.review_status=approved` as permission to create/build a product even though the current business decision only means “continue researching this opportunity.”

### Required next-gen fix

Create an explicit durable Product Definition concept with its own lifecycle and human gate. At minimum it should carry the facts defined in `CODEX_NEXT_OBJECTIVE.md`: target user, core problem/motivation, product form, modules/functions, deliverables, evidence-backed differentiation, exclusions, risks and human approval.

The runtime may prepare a candidate definition; it must not self-approve it.

## P0 — Code existence, business proof and authorization are currently easy to conflate

The repository contains radar, content and media code, while the current business handoff deliberately keeps B/C/D behind later gates.

A future AI entering the codebase could infer “`/content` exists, therefore content is the next step.” That would be wrong.

### Required next-gen fix

Make stage authority explicit and machine-readable:

```text
implemented_capability
proven_capability
current_business_stage
authorized_transition
```

The workbench should display these separately.

## P1 — No unified orchestration / Agent Runtime

Current services implement explicit call sequences. There is no general runtime with:

- AgentRun / AgentStep persistence;
- business Tool Registry;
- tool metadata such as read-only/destructive/concurrency/idempotency;
- central PermissionPolicy;
- bounded model/tool loop;
- step/model/token/wall-time budgets;
- Context Builder;
- checkpoint/resume ledger;
- generic hooks/timeline.

This limits how naturally the system can grow from Phase A into a unified A→B→C→D workbench.

### Required next-gen fix

Add one reusable orchestration layer **above domain services**. Do not convert adapters or SQLite into an LLM-controlled free-for-all.

## P1 — Context construction is too expensive

The current real successful cross-account analysis reported `243,276` prompt tokens for two accounts / 22 eligible evidence facts.

The current service serializes all resolved evidence plus the full output schema into the request. This is safe but inefficient.

### Required next-gen fix

Introduce a traceable Context Builder:

- raw evidence remains immutable;
- structured compact facts reference source evidence IDs;
- retrieve only facts relevant to the current decision;
- cache/reuse stable summaries where safe;
- expose token accounting before/after optimization;
- never allow compaction to upgrade evidence trust or remove the validators that re-resolve current evidence before commit/approval.

The 243k-token run should become a fixed comparison benchmark.

## P1 — Several core modules have become very large

Approximate current sizes from the audited branch include:

- `backend/app/db.py` — ~232 KB;
- `backend/app/features/analysis/service.py` — ~96 KB;
- `backend/app/features/xhs/service.py` — ~91 KB;
- `backend/app/features/content/service.py` — ~90 KB;
- `backend/app/features/content/cleanup.py` — ~74 KB;
- `backend/app/features/shops/service.py` — ~65 KB.

Large modules are not automatically bad, and these files contain valuable hardening. The risk is that evidence rules, persistence, orchestration and feature logic become difficult to change independently.

### Required next-gen fix

Split by stable responsibility only after tests protect behavior. Possible boundaries:

```text
analysis/
  context.py
  grounding.py
  evidence_resolver.py
  opportunity_policy.py
  service.py

db/
  engine.py
  migrations/
  integrity/
  repositories/
```

Do not refactor merely for aesthetics; migrate one protected behavior at a time.

## P1 — Current frontend is a domain dashboard, not an extensible workbench shell

The existing React UI is functional but small and hand-routes from `window.location.pathname`.

It lacks generic primitives for:

- Project → Run → Step hierarchy;
- Agent/tool timeline;
- permission/human-action inbox;
- evidence lineage view;
- current checkpoint and resume controls;
- model/token/budget observability;
- B/C/D workflow transitions;
- multiple specialized Agents/roles.

### Required next-gen fix

Evaluate licensed open-source workbench shells. Preserve current domain screens as behavioral references or embedded views where useful. Do not select a UI solely on appearance.

## P1 — Current business state is distributed across domain records and documents

Phase A has Jobs, evidence, Analysis and Opportunity state. Future B/C/D will add Product Definition, build, finished-product and content states.

Without a unified Project/Run model, the operator has to infer the overall business journey from several pages and records.

### Required next-gen fix

Introduce a top-level workbench model such as:

```text
Project
  ├─ Stage
  ├─ Run(s)
  ├─ HumanDecision(s)
  ├─ Evidence references
  └─ Deliverables
```

This should reference existing domain records rather than replace their truth.

## P2 — Physical/browser automation remains inherently fragile

Qianfan/XHS/Android depend on authenticated sessions, current layouts, selectors, device connectivity and platform behavior. This is not fully removable through architecture.

Current fail-closed behavior is therefore a strength, not a defect.

### Next-gen rule

Improve observability, tool availability checks and human takeover, but do not promise unattended recovery from login/captcha/layout/device problems. No automatic replay of an interrupted unsafe physical action.

## P2 — Reference archive is not yet complete

Current Git contains the readable tutorial text and structured digests of the two shared ZIPs, but not every original byte/file:

- original large SingleFile HTML is not physically archived in Git;
- full raw extracted trees of the demand-radar and content-system ZIPs are not yet committed; current `REFERENCE_DIGEST.md` files are summaries/navigation.

### Required follow-up

Before a claim depends on an exact original script/agent file, archive or inspect the original source material rather than treating a digest as the raw implementation.

## P2 — Full baseline verification should be rerun before a rewrite freeze

The UAT report records very strong focused regressions and many live validations, but historical whole-backend runs also recorded a small number of known failures outside the modified path.

### Required next-gen fix

Before migration implementation begins:

1. run the current baseline verification suite locally;
2. record current green/red baseline explicitly;
3. freeze representative fixtures and read-only real evidence references;
4. use them as parity tests for the new architecture.

Do not claim “everything is green” from historical focused test counts alone.

## Historical problems already solved or intentionally retained

These are not reasons to redesign the system from scratch, but they must remain regression cases:

- selector/grid ordering bugs;
- disconnected Android device;
- short shop / natural-end semantics;
- incomplete third-product evidence;
- stale backend process;
- provider timeout/retry exhaustion;
- false-positive broad cross-account demand;
- evidence-grounding rejection;
- Windows XML historical newline/hash mismatch;
- uncertain file/database commit ownership.

The new system is worse if it has cleaner code but loses the protections learned from these failures.

## Core conclusion

The present system's biggest weakness is **not that the backend is unreliable**. Its main architectural gap is that a hardened Phase A domain system, some future content infrastructure and a simple UI have grown without a single reusable orchestration/workbench model for the complete A→B→C→D journey.

The next-generation design should therefore be an integration and orchestration redesign, not a blind rewrite of proven evidence/business logic.
