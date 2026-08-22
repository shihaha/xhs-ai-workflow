# ADR-005: Product Definition is a separate human-authority gate

- Status: accepted
- Date: 2026-08-23

## Context

The current business architecture says an approved Opportunity authorizes **continued product research**, not a concrete product build. The repository already contains Product/content code whose existing Product record is thinner than the complete Product Definition required by the current handoff.

A generalized Agent Runtime would otherwise be at risk of interpreting `Opportunity.review_status=approved` as permission to create/build a product.

## Decision

Before automating the B -> C transition, introduce a durable Product Definition concept with its own review lifecycle.

Required semantics include at least:

- target user;
- core problem / purchase motivation;
- use scenarios;
- product form;
- modules/functions/content structure;
- deliverables;
- evidence-backed differentiation;
- out-of-scope items;
- risks/copyright/platform constraints;
- supporting research/evidence references;
- review status and human decision.

The Agent may research, draft and revise a Product Definition. It cannot self-approve it.

Only a current human-approved Product Definition may authorize Product Build.

Finished Product + human UAT remains a separate gate before content production.

## Alternatives considered

- treat approved Opportunity as product approval — rejected because it collapses the intentional Gap 1.
- reuse existing thin Product record as Product Definition — rejected unless it is explicitly expanded/migrated to represent the full semantic contract.
- keep the distinction only in prompt/docs — rejected because Agent orchestration requires a server-enforced durable gate.

## Consequences

Positive:

- prevents accidental A -> C jump;
- makes the tutorial's human role explicit in data/API/UI;
- gives the future workbench a clear B-stage deliverable;
- allows Product research to be agent-assisted without authorizing build.

Cost:

- new schema/API/UI lifecycle required before B/C automation;
- existing Product/content code must be revalidated against this boundary.

## Evidence / references

- `docs/CODEX_NEXT_OBJECTIVE.md`
- `docs/architecture-knowledge/01_TUTORIAL_BUSINESS_MODEL.md`
- `docs/architecture-knowledge/04_CURRENT_SYSTEM_PROBLEMS.md`
- `docs/architecture-knowledge/08_BACKEND_REQUIREMENTS.md`