# 20 — Workbench ChatGPT Handoff Read Projection

Status: implementation slice

## Purpose

Put the already validated durable manual-ChatGPT handoff record into the same ancestry as the Workbench read projections, then expose a redacted task/lifecycle view for the local React workbench.

This is not a redesign of the handoff bridge. The bridge implementation and its focused tests are folded in byte-for-byte from the dynamically accepted PR #14 head.

Validated source blobs:

- `backend/app/agent_runtime/manual_chatgpt_handoff.py`: `88c4dd18a9f55325cd76f22790f9799f82205e23`
- `backend/tests/agent_runtime/test_manual_chatgpt_handoff.py`: `c2eaf16b3a22838d1b63bfe8695d0d0e4bdb584d`

PR #14 source head `98de1319b0fdaaa1781ac9f4761023d2c1e8a71c` passed dedicated handoff verification run `32634318081` and broad Agent Runtime regression run `32634318100`.

## Read surfaces

- `GET /api/v1/agent-runtime/chatgpt-handoffs`
  - optional `status=pending|accepted`;
  - durable handoff identity and lifecycle only.

- `GET /api/v1/agent-runtime/chatgpt-handoffs/{handoff_id}`
  - one durable handoff lifecycle projection;
  - 404 for an unknown durable handoff.

The generic HumanAction list additionally accepts `status=completed`, because the validated manual-ChatGPT bridge resolves a returned handoff by marking its HumanAction `completed`; normal approval actions continue to use their existing statuses.

## Workbench fields

The handoff projection exposes enough durable state for a UI to decide whether to render `需要 ChatGPT 处理` without making the UI authoritative:

- `handoff_id`;
- authoritative `job_id`;
- `source_run_id`;
- `human_action_id`;
- handoff / HumanAction / Job lifecycle state;
- `stage_revision` and `schema_version`;
- immutable `input_hash` identity;
- context-ref count only;
- whether a result exists;
- whether the source run is still the unique latest binding;
- `authority_ambiguous`;
- `needs_chatgpt`;
- `result_ready`;
- creation / acceptance timestamps.

`needs_chatgpt=true` is deliberately strict. It requires all of these durable facts to agree:

- handoff is `pending`;
- linked HumanAction identity matches this exact handoff and is `pending`;
- source AgentRun is the unique latest Job binding;
- source AgentRun is `needs_human/manual_chatgpt_required`;
- authoritative Job is lease-free `needs_human/manual_chatgpt_required`.

`result_ready=true` similarly requires an accepted handoff, completed HumanAction, unique current binding, source AgentRun `manual_chatgpt_result_ready`, and lease-free Job `manual_chatgpt_result_ready`.

If relationship identity is broken or latest binding is ambiguous, the projection never claims that ChatGPT work is currently authoritative.

## Redaction boundary

The generic Workbench handoff API does **not** expose:

- `task_json`;
- raw `context_refs_json`;
- result-contract JSON;
- package path;
- return/result path;
- accepted `result_json`;
- raw HumanAction request/resolution JSON;
- raw Job input;
- cookies, credentials, browser profile, Android state, or provider prompts.

The outbound task package remains an explicit durable AgentDock transport capability. It is not widened into the generic list API.

## Continuation boundary

This read projection does not change the accepted-result continuation boundary. The validated bridge still persists the result/evidence first and leaves the Job `needs_human/manual_chatgpt_result_ready` until a separate authoritative continuation driver re-claims it.

The Workbench must never interpret `result_ready` as permission to mutate Job state itself.

## Projects boundary

There is still no authoritative durable Project entity in this ancestry. No Project is inferred from Job input, folders, display names, or UI state.

## B/C and physical-worker boundary

No generic B product-research workflow and no generic C product-build workflow is introduced.

No Android, XHS, or browser physical worker becomes an Agent Tool. The authority path remains:

`Agent -> durable Job / JobService -> physical worker -> Evidence / Artifact / Job state`.

## Acceptance

The dedicated gate must prove:

- the folded handoff source/test blobs still equal the already validated PR #14 blobs;
- the validated manual-handoff tests remain green in the Workbench ancestry;
- pending handoff projection reconstructs after app restart;
- accepted result projects as `result_ready` and HumanAction `completed`;
- raw task/context/result/path data do not leak;
- existing Workbench v1/v2 projections remain green;
- existing Jobs API tests remain green;
- complete Agent Runtime acceptance remains green.

The repository-wide Agent Runtime regression workflow is also required. No merge is automatic.
