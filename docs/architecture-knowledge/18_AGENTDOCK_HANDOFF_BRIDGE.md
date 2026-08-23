# 18 — AgentDock ChatGPT Handoff Bridge

Status: implementation contract

This refines `17_MANUAL_CHATGPT_HANDOFF.md` into a staged local-file bridge.

## Canonical local paths

- outbound package: `runtime/chatgpt-handoffs/outbox/<handoff_id>.json`
- AgentDock return inbox: `runtime/external-results/<handoff_id>.json`

The workbench, not ChatGPT, chooses `handoff_id` and the exact paths. AgentDock writes only the structured result envelope for that ID.

## Slice 1 — durable request + automatic return acceptance

The first implementation slice may stop after **durably accepting** the returned ChatGPT result. Acceptance must atomically persist the external-result step/evidence, resolve the dedicated handoff HumanAction, and mark the handoff accepted. The authoritative Job remains `needs_human` in this slice.

This is intentional fail-closed staging: no Job is re-claimed until the external-result continuation worker/composition root exists to actually drive the new AgentRun. Creating a `running` Job with no driver would merely create a lease-expiry failure.

## Slice 2 — accepted result continuation

A following slice will atomically:

1. verify the accepted handoff still belongs to the single current Job binding;
2. derive the remaining Job-wide Agent budget;
3. re-claim `needs_human -> running`;
4. create a new continuation AgentRun without replaying any Tool;
5. expose the accepted external-result step through `JobHistoryContextBuilder`;
6. hand the new run to the real Agent orchestration driver.

Only Slice 2 changes Job state away from `needs_human`.

## Return envelope

The AgentDock-written JSON envelope is bounded and self-identifying:

```json
{
  "handoff_id": "...",
  "schema_version": "...",
  "input_hash": "sha256:...",
  "result": {}
}
```

The initial result contract supports deterministic top-level required fields and an `allow_extra_fields` policy. Nested/provider-specific schema validation can be added later without weakening identity/hash checks.

## Acceptance gates

A result is accepted only if all of these remain true at the authoritative write boundary:

- exact handoff ID;
- exact schema version;
- exact input hash;
- result contract satisfied;
- handoff still pending;
- dedicated HumanAction still pending and identity-matched;
- source AgentRun still `needs_human` for `manual_chatgpt_required`;
- source run is the single latest Job binding;
- Job is still `agent_orchestration` + `needs_human` with no lease;
- no duplicate/stale replay.

Acceptance does not grant Android/XHS/browser authority and does not execute physical actions.
