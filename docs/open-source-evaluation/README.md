# Open-Source Evaluation

This directory evaluates candidate projects for the next-generation Xiaohongshu workbench.

Do not select a repository because its UI is attractive or because it has many stars. Evaluate it against our verified requirements.

## Candidate categories

- Workbench UI / task console.
- Agent Runtime / tool-use loop.
- Workflow and durable job orchestration.
- Context / memory infrastructure.
- Browser or computer-use integration.
- MCP / tool integration.
- Observability and run timeline.

## Required evaluation fields

For every candidate record:

- Repository and URL.
- Date inspected.
- License and commercial-use implications.
- Activity / maintenance status.
- Technology stack.
- Architecture summary.
- Features relevant to our requirements.
- Features we do not need.
- Integration cost.
- Risk of duplicate state machines or duplicate persistence.
- Ability to isolate UI/runtime components.
- Fit with the existing XHS evidence and job model.
- Decision: shortlist / reference only / reject.
- Exact rejection reason when rejected.

## Selection principle

Prefer a small number of clearly owned layers over merging multiple large frameworks that each try to own jobs, state, permissions, persistence, and UI.
