# Architecture Knowledge

This directory stores durable, project-specific understanding of the current Xiaohongshu system and the target workbench architecture.

## Required contents

- Project map and authoritative documents.
- Tutorial/business workflow decomposition.
- Current frontend/backend/database/job/evidence architecture.
- Proven capabilities versus incomplete or experimental capabilities.
- Known failure modes, UAT findings, technical debt, and migration constraints.
- Derived requirements for Agent Runtime, workbench UI, backend orchestration, permissions, context, checkpoints, and observability.
- Migration rules and implementation roadmap.

## Quality standard

Each document should distinguish:

1. Verified facts from the repository or real runs.
2. Inferences that still require validation.
3. Design proposals.
4. Decisions already accepted.

Whenever possible, reference exact files, modules, tests, job states, database entities, evidence IDs, or real run results rather than vague summaries.

This directory is the durable project memory for future implementation sessions.
