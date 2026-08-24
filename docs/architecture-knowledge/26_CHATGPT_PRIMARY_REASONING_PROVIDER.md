# 26 — ChatGPT as the Primary External Reasoning Provider

Status: authoritative architecture override
Date: 2026-08-24

## Decision

For the current XHS workbench, **ChatGPT is the primary external reasoning provider for operator-facing Agent work that requires interpretation, planning, classification, synthesis, or other non-deterministic judgment.**

Alibaba Bailian and any local/model-provider adapter remain optional accelerators. Their absence is **not** a configuration failure for the main workflow and must not by itself block progress.

This decision supersedes earlier wording that treated Bailian configuration as mandatory for production language-model reasoning or approval continuation admission.

## Runtime split

The intended authority path is:

```text
Deterministic rule/domain code can decide
  -> decide locally

Deterministic rule/domain code cannot decide
  -> durable ChatGPT handoff
  -> ChatGPT reads bounded existing Evidence / Artifact references
  -> ChatGPT returns a structured result that satisfies a durable result contract
  -> result is accepted and persisted first
  -> a separate authoritative continuation/resolution step consumes that accepted result
```

The local system still owns:

- Job lifecycle and leases;
- AgentRun / AgentStep / HumanAction durability;
- Evidence and Artifact integrity;
- Android/XHS/browser/Qianfan physical execution;
- deterministic validators and business gates;
- cancellation, recovery, no-replay and fail-closed rules.

ChatGPT may provide:

- ambiguous shop-scope classification;
- evidence interpretation;
- analysis and synthesis;
- planning / next-step selection within allowed capabilities;
- Product Definition/content reasoning in later authorized stages;
- structured decisions that local deterministic code can validate and persist.

ChatGPT does **not** gain direct physical-worker authority and does not replace JobService or domain services.

## Stage 6 implication

A real Android preflight that successfully collects representative products but ends with `scope_evidence_ambiguous` must not be treated as "missing Bailian configuration".

The intended next state is a durable **ChatGPT-needed** handoff/resolution path using the already collected evidence. The physical collection must not be replayed merely to obtain the reasoning result.

For example:

```text
shop.preflight
  -> durable Android Job
  -> 3/3 representative products collected
  -> deterministic scope classifier = ambiguous
  -> durable ChatGPT handoff using those artifacts/evidence
  -> ChatGPT structured scope decision
  -> persist accepted result
  -> continue domain workflow without repeating Android side effects
```

## Bailian compatibility

Existing `BailianModelAdapter` code and historical UAT remain valid assets. They may still be used when explicitly configured and useful, but new work must not assume:

- a Bailian API key exists;
- Bailian is the only permitted reasoning route;
- missing Bailian means `approve_continuation=false` for every workflow;
- ambiguous evidence must stop at generic manual review when a ChatGPT handoff can carry the reasoning safely.

Provider-neutral interfaces should remain provider-neutral.

## Safety / durability requirements

1. ChatGPT receives only bounded task packages and allowed evidence references.
2. Raw credentials, cookies, local secret state and unrestricted filesystem access are never packaged.
3. ChatGPT result identity is pinned by handoff ID, schema version and input hash.
4. Accepted result is persisted before continuation.
5. Accepted ChatGPT output is reasoning input, not business truth by itself; domain validation still applies.
6. Terminal Job state cannot be silently rewritten.
7. Unsafe or uncertain physical side effects are never replayed as part of ChatGPT reasoning recovery.
8. If an interrupted approved physical action has any durable Tool execution proof, recovery remains fail-closed.

## Implementation consequence

Stage 6 should finish by connecting physical preflight ambiguity to the existing durable manual-ChatGPT handoff/result bridge, then add an authoritative no-replay resolution/continuation path. A fake local proposal/finish model is acceptable only for controlled UAT of permission plumbing; it is not the production reasoning architecture.
