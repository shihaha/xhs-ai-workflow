# 17 — Manual ChatGPT Handoff

Status: architecture decision / implementation constraint

## Decision

Some reasoning-heavy or quality-sensitive stages may be handled poorly by the default Bailian model. The workbench must therefore support a **manual external expert handoff** in which ChatGPT is used by the operator outside the workbench, rather than being embedded as an automatic provider.

This is a deliberate product mode, not a temporary workaround.

The **canonical return path is AgentDock automatic return into the local workbench**. Copy/paste is not the normal product flow and should not be presented as the primary UX.

## Required behavior

When a stage is configured for manual ChatGPT handling, the system must not silently fall back to Bailian. It should:

1. stop the authoritative Job at a human-wait boundary;
2. persist a handoff record with a unique `handoff_id`;
3. generate a bounded handoff package containing the exact task, required evidence/context references, relevant files/inputs, and expected output schema;
4. surface a clear UI prompt such as `需要 ChatGPT 处理`;
5. let the operator send that handoff package to ChatGPT in a normal ChatGPT conversation;
6. when that conversation exposes AgentDock, let ChatGPT write the structured result back automatically to the local workbench inbox;
7. accept the returned structured result only if it matches the current `handoff_id`, expected schema, stage revision/input hash, and still-authoritative Job state;
8. persist the accepted result/evidence before the Job continues.

The system must never treat a ChatGPT answer as authority for a different or stale task.

## Canonical AgentDock return bridge

When ChatGPT has AgentDock access, ChatGPT writes a structured result file into a local inbox such as:

`runtime/external-results/<handoff_id>.json`

The local workbench watches/imports this inbox. Import must validate all of the following before accepting the result:

- exact `handoff_id`;
- expected output schema/version;
- input hash or stage revision;
- still-authoritative Job / continuation state;
- result file is not a duplicate or stale replay.

After validation, the workbench persists the accepted result and any derived evidence, records an audit event, and only then resumes the Job.

ChatGPT must not receive direct physical-worker authority through this bridge. Android/XHS/browser actions remain behind the existing durable Job/worker boundary.

## Copy/paste policy

Manual paste/import may exist only as an emergency recovery/debug path. It is **not** the default operator workflow and should not be the main UI path while AgentDock is available.

## Local runtime decision

The current product should be designed for a **local Windows runtime** as the primary deployment model because:

- Android/XHS physical workers are already local;
- SQLite/runtime artifacts are local;
- AgentDock can operate on local files and commands when the conversation exposes that connector;
- the external-result inbox can stay on disk without exposing a localhost API to the public internet.

The UI remains a browser-based React workbench served from localhost. Local runtime does **not** require a native desktop UI.

A hosted deployment may be supported later, but it must preserve the same handoff identity, schema, authority, and replay-safety rules.

## Provider policy

Treat model choice as a stage-level policy rather than a global setting. Example modes:

- `bailian_auto` — default automatic model execution;
- `manual_chatgpt` — stop and create a ChatGPT handoff package, then return via AgentDock;
- future providers may be added without changing Job semantics.

Quality-sensitive stages may default to `manual_chatgpt` even if Bailian is available.

## Non-goals

- Do not embed the user's ChatGPT subscription as if it were an API.
- Do not let the model choose arbitrary local files or Job IDs.
- Do not bypass Job authority, evidence requirements, approval gates, or CAS checks.
- Do not turn B/C product research/build into generic workflows; this handoff mechanism is a generic execution boundary only.
