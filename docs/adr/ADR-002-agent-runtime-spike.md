# ADR-002: Use Pydantic AI/Harness for the first Agent Runtime spike

- Status: proposed (approved for isolated spike; production adoption depends on spike acceptance)
- Date: 2026-08-23

## Context

The runtime requirements call for typed tools/actions, approval deferral, hooks, budgets, context management and optional specialized agents while preserving our Python/Pydantic/FastAPI stack.

Reconstructed Claude Code demonstrates the desired architectural discipline but is not a safe code dependency. Among licensed current frameworks, Pydantic AI + Pydantic AI Harness has the closest fit with the fewest forced architectural changes.

## Decision

Build the first isolated Agent Runtime experiment using Pydantic AI and selected Harness capabilities.

The experiment must still implement original project-specific components:

- XHS `ToolRegistry` facade over domain services;
- `PermissionPolicy` with business authority;
- `ContextBuilder` with evidence-linked compact facts;
- AgentRun/AgentStep/checkpoint persistence;
- existing Job integration;
- unsafe physical-action resume policy.

Do not connect real Android/Qianfan/Opportunity approval in the first slice.

Production adoption becomes accepted only after the spike satisfies `06_AGENT_RUNTIME_REQUIREMENTS.md` acceptance tests and does not weaken existing domain regression tests.

## Alternatives considered

- LangGraph — strong checkpoint/durable graph engine, but introduces graph semantics before a simple bounded loop is proven.
- Microsoft Agent Framework — strong production feature set, but broader/heavier for current local V1.
- OpenHands SDK — mature but coding-agent/workspace-centric.
- AutoGen — current upstream is in maintenance mode.
- reconstructed Claude Code source — not license-eligible.

## Consequences

Positive:

- typed Python integration with current code;
- built-in/declarative patterns for deferred approvals and hooks;
- strong context/usage primitives available;
- low cost of removal if our own Tool/Permission/Context interfaces remain independent.

Risks:

- library APIs are evolving and must be version-pinned;
- Harness features must not import coding-agent assumptions into domain logic;
- production model routing still must respect current Bailian policy;
- framework message history must never become evidence truth.

## Evidence / references

- `docs/source-research/PYDANTIC_AI_HARNESS_STUDY.md`
- `docs/source-research/CLAUDE_CODE_2_1_88_ARCHITECTURE_STUDY.md`
- `docs/architecture-knowledge/06_AGENT_RUNTIME_REQUIREMENTS.md`
- `docs/architecture-knowledge/10_OPEN_SOURCE_CANDIDATES.md`