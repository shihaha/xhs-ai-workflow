# ADR-006 — Defer B/C workflow systemization

- Date: 2026-08-23
- Status: ACCEPTED
- Supersedes: the Stage 8 / Stage 9 systemization assumptions in `docs/architecture-knowledge/12_IMPLEMENTATION_ROADMAP.md`

## Context

The business journey still uses the conceptual A/B/C/D labels:

```text
A — Opportunity / demand discovery
B — Product research and concrete product definition
C — Product creation / build
D — Content research and production for acquisition
```

However, B and C are not currently stable workflows.

Their steps vary materially by product type. A psychological test, spreadsheet template, PDF/document product, website, mini-program, software tool, course, or another digital product can require completely different research questions, artifacts, tools, validation, and build processes.

We do not yet have enough completed products across different categories to distinguish genuinely reusable B/C patterns from one-off product-specific behavior.

Building a generalized B/C workflow now would therefore encode guesses as architecture and would increase scope without evidence.

## Decision

**Do not systemize B or C at this stage.**

The workbench may represent the handoff boundaries around B/C, but it must not implement a generic B Agent, C Agent, B/C state machine, B/C workflow engine, universal ProductResearchAgent, universal ProductBuildAgent, or generic builder orchestration merely to complete an A/B/C/D diagram.

Current intended journey:

```text
A system
  Opportunity
      ↓
  human approval
      ↓
────────────────────────────────────────
B — external / human + AI, product-specific
  output: Product Definition
────────────────────────────────────────
      ↓
────────────────────────────────────────
C — external / human + AI / Codex / other tools,
    product-specific
  output: Finished Product
────────────────────────────────────────
      ↓
  human UAT / product acceptance
      ↓
D system
  Content research / production
```

## What the workbench may store now

Only thin, durable handoff facts are justified:

### Product Definition handoff

A durable record/reference that answers at minimum:

- which approved Opportunity it came from;
- what concrete product was chosen;
- where the human-approved definition/brief lives;
- when/who approved it;
- optional evidence/reference links needed for audit.

This record is a **handoff artifact**, not a B workflow engine.

### Finished Product handoff

A durable record/reference that answers at minimum:

- which Product Definition it implements;
- where the finished deliverable lives;
- product type / artifact kind;
- UAT/acceptance status;
- optional manifest/hash/evidence references appropriate to the artifact.

This record is a **handoff artifact**, not a C workflow engine.

## Explicit non-goals

Until this ADR is revisited, do not add:

- `ProductResearchAgent`;
- `ProductBuildAgent`;
- generic B/C task graphs;
- generic B/C orchestration rules;
- a universal product builder;
- per-stage B/C automation merely to make the workbench look complete;
- assumptions that one product's B/C process is reusable for another product type.

## When to revisit

Revisit only after enough real products have been completed that recurring patterns can be demonstrated from evidence rather than guessed.

The trigger is not a fixed date or an arbitrary architecture milestone. The trigger is a sufficiently varied corpus of completed B/C cases from which we can answer:

1. which steps recur across product types;
2. which steps recur only within a product family;
3. which decisions always require a human;
4. which artifacts have stable schemas;
5. which tools/builders can safely be reused;
6. which failure modes and UAT gates recur.

Only then should reusable B/C workflow primitives be extracted.

## Consequences

### Positive

- keeps current implementation focused on the proven A system, Agent runtime/workbench infrastructure, and D integration;
- avoids speculative workflow architecture;
- lets each early product use the best product-specific tools/process;
- preserves real completed cases as future training/design evidence;
- prevents scope growth from delaying useful operation of the workbench.

### Trade-off

The workbench will not initially automate the entire A→B→C→D chain end-to-end. B/C require an explicit human/external-tool handoff.

This is intentional. A truthful partial workflow is preferred over a fictitious generic workflow.

## Relationship to ADR-005

ADR-005 remains valid in its authority rule:

> Opportunity approval does not authorize building a product.

But the implementation interpretation changes: the system does **not** need to model the internal research/build process. It only needs durable human-approved handoff facts so that A cannot silently authorize C, and D cannot start without an accepted Finished Product.

## Architecture rule

Future implementation agents must treat this ADR as the current authority over older roadmap text. If older documents describe Stage 8/9 B/C workflow systemization, this ADR overrides those sections until explicitly superseded by a later accepted ADR.