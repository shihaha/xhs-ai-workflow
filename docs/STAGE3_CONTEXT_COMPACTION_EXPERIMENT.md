# Stage 3 — Grounded Model Context Compaction Experiment

Status: validated isolated spike; not a production-path adoption.

Branch: `spike/context-compaction-v1`
Base: Stage 2 head `e74181c2eadd485fd6a70d00f02a4bb7fb8b517f`

## Why this experiment exists

The bounded real provider verification recorded in `PROJECT_STATUS.md` succeeded with:

- model: `deepseek-v4-flash`
- prompt contract: `tutorial-demand-radar-specific-demand-v2`
- prompt tokens: **243,276**
- completion tokens: 4,984
- provider duration: 45,797 ms

The current analysis path is evidence-correct, but inspection found two avoidable sources of model-input bulk:

1. `AnalysisService` sends the entire trusted `ShopCollectionRead` into `allowed_evidence`, including opaque per-product `raw_evidence`, artifact paths, hashes, manifests and audit-only counters.
2. `AnalysisService` embeds `AnalysisOutput.model_json_schema()` in the user prompt while `BailianModelAdapter` independently embeds the same schema in the system message.

Account-note public facts are already a comparatively compact normalized projection and are not aggressively summarized in this spike.

## Safety invariant

**Compaction must never become a trust decision.**

The full persisted evidence remains authoritative for:

- evidence resolution and ownership;
- SHA/file/SQLite trust checks;
- opportunity eligibility;
- `input_digest` and trust fingerprinting;
- final evidence snapshot;
- post-model schema and evidence grounding;
- cross-account demand validation;
- human approval gates.

The compact view exists only at the model-adapter boundary. It cannot add an evidence ID, upgrade eligibility, synthesize trust, change the account scope, or replace raw persisted evidence.

## Spike design

`CompactingModelAdapter` decorates an existing model adapter. It receives the already-built `StructuredModelRequest`, proves that the user-prompt `required_schema` exactly equals the schema supplied to the adapter, removes that duplicate copy, and replaces `allowed_evidence` with a deterministic model view.

### Account notes

Keep the existing public fact unchanged in meaning:

- evidence ID and account owner;
- public profile identity, URL, nickname, bio and public counters;
- note identity, URL, title, summary, publish time and public interactions.

### Rank items

Keep normalized business fields and source identity. Remove opaque `raw_evidence` from the model view.

### Trusted shop results

Keep:

- evidence ID and account owner;
- job identity/state;
- collection status/mode and bounded completeness counters;
- sample/shop/natural-end flags and scope conclusion;
- normalized product `id`, `kind`, `source_url` and complete normalized `data`;
- compact verification counts and `complete` flag.

Remove from model view only:

- item `raw_evidence`;
- artifact-path lists;
- manifest/collection paths and SHA values;
- rejected/missing raw audit blobs;
- other audit-only transport/debug metadata already verified before the model call.

Nothing is deleted from SQLite or evidence files.

## Acceptance criteria

The spike is acceptable only if all of the following hold:

1. Evidence IDs are preserved exactly and in input order.
2. Account ownership is preserved for every projected fact.
3. Normalized product data remains available to the model.
4. Original trusted facts are not mutated.
5. The duplicated output schema is removed only after exact equality is proven; mismatch fails closed.
6. A representative high-bloat synthetic fixture reduces serialized user-prompt characters and UTF-8 bytes by **at least 80%**.
7. Character/byte savings are labelled structural metrics, not provider-token estimates.
8. A controlled `AnalysisService` A/B produces the same business signature with and without the decorator.
9. Existing Stage 2 Agent Runtime acceptance remains green.
10. No newly reproducible backend regression appears relative to Stage 2.

## Verification result — 2026-08-23

Stage 3 passed its intended verification boundary.

- focused context-compaction acceptance: **5/5 passed**;
- Stage 2 Agent Runtime acceptance on the same PR merge ref: **23/23 passed**;
- Stage 2 full-backend baseline: **73 failed / 1,444 passed / 20 skipped**;
- Stage 3 full-backend spike: **71 failed / 1,451 passed / 20 skipped**;
- correct Stage 2 -> Stage 3 regression guard: **0 new candidates**;
- two timing-sensitive shop cancellation failures appeared only in the baseline full-suite run and therefore counted as fixed/non-regressing for this comparison;
- final Stage 3 backend regression guard: **success**.

The earlier guard that compared the Stage 3 PR against `research/xhs-workbench-next` was not a valid Stage 3-only comparison because it included all Stage 2 Runtime changes. The Stage 3 workflow was corrected to use `spike/agent-runtime-pydantic-v1` as its baseline and retained the same fail-closed targeted-recheck logic.

Conclusion: the isolated compaction layer is technically viable and did not introduce a newly reproducible backend regression relative to Stage 2. This does **not** authorize production wiring or claim a provider-token reduction percentage.

## What this spike does not prove

It does **not** claim that the historic 243,276-token provider input is reduced by the same percentage. DeepSeek/Bailian tokenization must be measured by a real provider call against the same trusted evidence set.

A live A/B call is deliberately excluded from this free CI experiment because it consumes provider quota. It requires explicit authorization and must be bounded to one baseline/one compact comparison, or preferably reuse a previously captured baseline request if a byte-identical prompt can be reconstructed.

## Adoption gates after a successful spike

Before production adoption:

- the persisted prompt version must explicitly identify compact-context semantics rather than silently reusing V2;
- application wiring must opt into the decorator intentionally;
- the current real trusted evidence set should be replayed through a bounded provider A/B if cost is approved;
- Stage 3 must remain independent of the still-open `AgentRun -> durable Job job_id` integration gate required before autonomous physical XHS/Android work.
