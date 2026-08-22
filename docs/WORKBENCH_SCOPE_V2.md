# Workbench Scope V2 — A + D Unified Workbench

> Current scope for `feature/workbench-ui-v2`. This document records the current design decision without rewriting historical Phase-A handoff records.

## 1. System boundary

The unified workbench contains two business systems plus one operational center:

```text
A — Demand Radar
    Qianfan rankings
    → candidate accounts
    → real XHS/account/shop/note evidence
    → cross-account specific-demand analysis
    → Opportunity
    → human review

B — Product research / definition
    OUTSIDE THIS WORKBENCH
    Customized for the actual product and executed as an independent human/AI work segment.

C — Product construction
    OUTSIDE THIS WORKBENCH
    Customized for the approved product type and executed as an independent build/UAT project.

D — Content System
    Finished Product + passed human UAT
    → Finished Product Dossier
    → product-fact organization
    → 10–20 keyword network
    → benchmark collection
    → one-note decomposition
    → template clustering
    → human/AI finalized production Skill
    → daily generation
    → independent content/visual review
    → human approval
    → pending-publication package

Operations Center
    Jobs / needs-human / failures / evidence / artifacts / health
```

B and C are intentionally not generalized into backend state machines. The tutorial does not define a reusable engineering contract for them, and their correct process depends on the concrete product form.

## 2. UI foundation

The existing React/Vite application remains the product frontend. Selected layout/workbench patterns are adapted from `satnaing/shadcn-admin`; the existing FastAPI APIs, SQLite truth, evidence rules, routes and business pages remain authoritative.

Current migration rule:

- do not replace the repository with shadcn-admin;
- do not introduce a second source of truth;
- do not upgrade React/Vite/TypeScript merely to match the template;
- preserve existing page/API tests;
- shell/layout styles stay isolated from existing business-page styles;
- third-party attribution is recorded in `THIRD_PARTY_NOTICES.md`.

## 3. Phase-D entry contract

Phase D starts from a **Finished Product Dossier**, never from an Opportunity.

A dossier is accepted only when `uat_status=passed` and records at least:

- stable product key and product version;
- real product name;
- target user;
- core need;
- actual deliverables;
- usage instructions;
- FAQ;
- allowed claims;
- forbidden claims;
- source-material index.

The content system does not infer missing product facts and does not create or modify the product itself.

## 4. Keyword-layout contract

The tutorial keyword-layout step is now represented as auditable keyword-plan runs.

Each run:

- belongs to one finished-product dossier;
- contains 10–20 distinct search terms;
- classifies terms into tutorial-derived dimensions;
- stores `expand`, `scope` and benchmark target count;
- is append-only at the application level; later runs do not delete earlier runs;
- records whether it was manual or AI-generated;
- AI runs record provider, model, prompt version, token usage and duration.

AI generation uses the existing configured Bailian text adapter. It may only use the finished-product dossier facts. Invalid model output fails closed and does not create a run.

## 5. Next Phase-D engineering sequence

Continue only through tutorial-defined content-system behavior:

```text
Finished Product Dossier       implemented in this branch
Keyword layout                 implemented in this branch
Benchmark collection binding   next
Single-note decomposition      next
Template clustering            next
Skill approval                 next
Daily generation               reuse/align existing content service
Independent review             reuse/align existing content/media review
Pending-publication export     reuse/align existing package export
```

Do not add B/C automation while completing these D steps.

## 6. Verification rule

Controlled software verification and live-platform UAT remain separate.

A code path is not considered live-verified merely because unit/integration tests pass. Real XHS/Qianfan/Android/Bailian collection must continue to record real jobs, evidence, artifacts and fail-closed states using the existing UAT rules.
