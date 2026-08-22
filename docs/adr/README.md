# Architecture Decision Records (ADR)

ADRs preserve why important architecture decisions were made so future sessions do not have to reconstruct the reasoning.

## Naming

Use sequential names such as:

- `ADR-001-agent-runtime-boundary.md`
- `ADR-002-evidence-ownership.md`
- `ADR-003-database-strategy.md`

## Template

```md
# ADR-XXX: Decision title

- Status: proposed | accepted | superseded | rejected
- Date: YYYY-MM-DD

## Context
What verified problem or constraint requires a decision?

## Decision
What are we choosing?

## Alternatives considered
What other options were seriously evaluated?

## Consequences
What becomes easier, harder, safer, or more constrained?

## Evidence / references
Which repository files, tests, runs, or external project research support this decision?
```

## Rule

Do not use ADRs for temporary implementation notes. Use them for decisions that future developers or AI agents are likely to question or accidentally reverse.
