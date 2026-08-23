# 17 — Manual ChatGPT Handoff

Status: architecture decision / implementation constraint

## Decision

Some reasoning-heavy or quality-sensitive stages may be handled poorly by the default Bailian model. The workbench must therefore support a **manual external expert handoff** in which ChatGPT is used by the operator outside the workbench, rather than being embedded as an automatic provider.

This is a deliberate product mode, not a temporary workaround.

## Required behavior

When a stage is configured for manual ChatGPT handling, the system must not silently fall back to Bailian. It should:

1. stop the authoritative Job at a human-wait boundary;
2. persist a handoff record with a unique `handoff_id`;
3. generate a bounded handoff package containing the exact task, required evidence/context references, relevant files/inputs, and expected output schema;
4. surface a clear UI prompt such as `需要 ChatGPT 处理`;
5. let the operator send that handoff package to ChatGPT in a normal ChatGPT conversation;
6. accept a returned structured result only if it matches the current `handoff_id`, expected schema, stage revision/input hash, and still-authoritative Job state;
7. persist the accepted result/evidence before the Job continues.

The system must never treat a copied/pasted ChatGPT answer as authority for a different or stale task.

## Two return paths

### Manual paste/import

The operator copies the ChatGPT result back into the workbench (or uploads a result file). This works whether the workbench is local or hosted.

### AgentDock local bridge

When ChatGPT has AgentDock access, ChatGPT may write a structured result file into a local inbox such as:

`runtime/external-results/<handoff_id>.json`

The local workbench may watch/import this inbox. Import must still validate `handoff_id`, schema, input hash/revision, and Job authority before accepting the result.

ChatGPT must not be given direct physical-worker authority through this bridge. Android/XHS/browser actions remain behind the existing durable Job/worker boundary.

## Local runtime recommendation

A local Windows runtime is the preferred deployment for the current product because:

- Android/XHS physical workers are already local;
- SQLite/runtime artifacts are local;
- AgentDock can operate on local files and commands when the conversation exposes that connector;
- a file-based external-result inbox is simple and does not require exposing a localhost API to the public internet.

The UI may still be a browser-based React workbench served from localhost. Local runtime does **not** require a native desktop UI.

This is a recommendation, not a hard architectural requirement. A hosted orchestrator can also support manual paste/import, but the AgentDock bridge is most natural when the authoritative runtime is local.

## Provider policy

Treat model choice as a stage-level policy rather than a global setting. Example modes:

- `bailian_auto` — default automatic model execution;
- `manual_chatgpt` — stop and create a ChatGPT handoff package;
- future providers may be added without changing Job semantics.

Quality-sensitive stages may default to `manual_chatgpt` even if Bailian is available.

## Non-goals

- Do not embed the user's ChatGPT subscription as if it were an API.
- Do not let the model choose arbitrary local files or Job IDs.
- Do not bypass Job authority, evidence requirements, approval gates, or CAS checks.
- Do not turn B/C product research/build into generic workflows; this handoff mechanism is a generic execution boundary only.
