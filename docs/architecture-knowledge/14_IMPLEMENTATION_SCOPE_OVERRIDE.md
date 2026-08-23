# 14 — Implementation Scope Override: B/C Are Handoffs, Not Workflows

- Date: 2026-08-23
- Status: ACTIVE OVERRIDE
- Authority: ADR-006
- Applies to: `12_IMPLEMENTATION_ROADMAP.md`

This document exists because the original roadmap still contains older Stage 7–9 assumptions about systemizing Product Definition (B) and Product Build (C). Those assumptions are no longer current.

## Current business shape

```text
A — Opportunity / demand discovery
        ↓
   human approval
        ↓
B — product-specific external work
        ↓
   Product Definition handoff
        ↓
C — product-specific external work
        ↓
   Finished Product + human UAT handoff
        ↓
D — Content research / production
```

## Current implementation scope

### Build now

- Agent Runtime reliability and canonicalization;
- AgentRun ↔ existing durable Job relationship;
- Context/token control;
- Project/Run/Human Action/Evidence workbench views;
- A-side orchestration over existing domain services;
- safe, permission-gated interaction with existing durable collection Jobs;
- thin durable Product Definition handoff;
- thin durable Finished Product/UAT handoff;
- D-side content integration after Finished Product acceptance.

### Do not build now

- generic Phase B workflow;
- generic Phase C workflow;
- ProductResearchAgent;
- ProductBuildAgent;
- universal product build brief generator;
- universal product builder;
- B/C graph orchestration;
- type-agnostic B/C state machine beyond the minimum handoff/acceptance facts required to protect authority gates.

## Replacement for old roadmap Stage 7–10

### Revised Stage 7 — Project + journey shell

Add a lightweight Project layer that references existing Opportunity, Job, AgentRun, evidence and content facts.

The Project view may show these milestones:

```text
Opportunity approved
Product Definition received/approved
Finished Product received/UAT accepted
Content eligible
```

The milestone UI is not permission to automate the internal B/C work.

### Revised Stage 8 — Handoff records only

Implement only the minimum durable boundary records needed to protect the chain:

```text
Opportunity
 -> Product Definition handoff
 -> Finished Product/UAT handoff
 -> D eligibility
```

A handoff record stores provenance, reference/location, human authority and acceptance state. It does not model how B or C was performed.

### Revised Stage 9 — D integration

Once a Finished Product has passed human UAT, connect the existing content research/production infrastructure to that accepted product dossier.

No automatic publication.

### Deferred — B/C workflow extraction

Only after enough varied real products have been completed should the project inspect those cases and extract recurring B/C primitives. That future work must begin from evidence of repetition, not from the tutorial diagram or a desire for end-to-end automation.

## Implementation rule

When this file conflicts with Stage 7–10 text in `12_IMPLEMENTATION_ROADMAP.md`, **this file and ADR-006 win** until explicitly superseded by a later accepted ADR.

Future agents should not spend implementation time designing B/C workflows unless the user explicitly reverses this decision.