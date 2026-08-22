# 03 — Current System Capabilities

- Audit date: 2026-08-23
- Rule: distinguish **implemented**, **real-UAT proven**, **business-active**, and **historical**. These labels are not interchangeable.

## Capability status vocabulary

- `PROVEN_REAL_UAT` — exercised against real local/platform/provider facts and preserved through readback/restart checks where applicable.
- `SOFTWARE_IMPLEMENTED` — meaningful production code and automated tests exist, but this audit does not claim the complete live business path is currently authorized/proven.
- `LATENT_OR_FUTURE_STAGE` — code exists, but current A/B/C/D business boundary says it is not the active next step or is missing an upstream gate.
- `HISTORICAL_ONLY` — retained as audit history and must not be treated as current eligible truth.

## Capability matrix

| Capability | Status | Current evidence / behavior | Next-gen disposition |
|---|---|---|---|
| Qianfan eight-scope collection model | `SOFTWARE_IMPLEMENTED` + real runs in UAT history | fixed four-board × two-dimension collection concept; snapshots persisted | preserve semantics; wrap as Tool later |
| Tutorial account scoring | `PROVEN_REAL_UAT` | literal GMV/payment/read weighting + recurrence/board/follower factors; stable ordering used on 71 candidates | preserve formula unless user explicitly changes business rule |
| Candidate stable-order continuation | `PROVEN_REAL_UAT` | can continue beyond Top 20 without direction-targeted replacement | preserve |
| Low-cost digital/physical prescreen | `PROVEN_REAL_UAT` | 71 classifications persisted and hash/readback checked in UAT history | preserve fail-open `uncertain` semantics |
| Android shop preflight | `PROVEN_REAL_UAT` | final business-scope gate with explicit physical/needs-human outcomes | preserve; never auto-replay unsafe navigation |
| XHS account profile + latest-note sample | `PROVEN_REAL_UAT` | CDP path produced trusted account facts and bounded latest-10 samples | preserve adapter/domain split |
| Shop product discovery persistence | `PROVEN_REAL_UAT` | discovered product identities persisted before later deep failure; restart-readable | preserve strongly |
| Shop `evidence_sample` | `PROVEN_REAL_UAT` | first three distinct default-order products, or proven natural-end short shop; same-job evidence requirements | preserve exact business semantics |
| Artifact / source / manifest / SHA chain | `PROVEN_REAL_UAT` | current eligible shop jobs have byte/hash/readback evidence; historical mismatches retained rather than rewritten | preserve as truth foundation |
| Durable Job state machine | `SOFTWARE_IMPLEMENTED` + heavily regression-tested | leases, CAS transitions, logs, artifacts, needs-human recovery, idempotent artifact attach | reuse/extend rather than replace |
| Adapter contracts | `SOFTWARE_IMPLEMENTED` | strict collection accounting, device/model/media contracts | reuse; future Tools should sit above adapters |
| Capability-based Adapter Registry | `SOFTWARE_IMPLEMENTED` | XHS CLI/CDP resolution by supported capability + priority | reuse/extend |
| Single-account / cross-account analysis framework | `PROVEN_REAL_UAT` | structured Bailian request, strict Pydantic output, evidence grounding | preserve validators; redesign context construction |
| Specific shared-demand contract | `PROVEN_REAL_UAT` | rejects broad shared channels/umbrella needs; current successful run found a specific shared demand | preserve |
| Before/after-model evidence trust revalidation | `SOFTWARE_IMPLEMENTED` + exercised in regressions | evidence is resolved before call and re-resolved in reserved transaction before success | preserve |
| Immutable analysis snapshot | `SOFTWARE_IMPLEMENTED` | DB triggers and service logic seal successful evidence graph | preserve |
| Opportunity review gate | `PROVEN_REAL_UAT` | pending → approved/rejected, one-way; approval revalidates current evidence and positive conclusion | preserve |
| Current `七宗罪` Opportunity | `PROVEN_REAL_UAT` | current analysis succeeded; Opportunity persisted and later approved for product research | keep as first next-gen replay/reference case |
| Product row / product materials | `SOFTWARE_IMPLEMENTED`, `LATENT_OR_FUTURE_STAGE` | Product can be tied to approved Opportunity; materials are managed/versioned/hashed | do not treat as complete Product Definition model; selectively reuse |
| Content generation/revision/review | `SOFTWARE_IMPLEMENTED`, `LATENT_OR_FUTURE_STAGE` | generation, revision CAS, human review, regeneration exist | revalidate against actual tutorial D workflow before migration |
| Deterministic content package export | `SOFTWARE_IMPLEMENTED`, `LATENT_OR_FUTURE_STAGE` | local pending-publication package; no publishing | likely reusable after Finished Product gate exists |
| Content media generation/vision checks | `SOFTWARE_IMPLEMENTED`, `LATENT_OR_FUTURE_STAGE` | provider adapters/workers exist | reuse only after D model is finalized |
| React operator UI | `SOFTWARE_IMPLEMENTED` | radar/account/opportunity/content/status/jobs pages | use as behavioral reference; UI shell may be replaced |
| Full generic Agent Runtime | **NOT IMPLEMENTED** | no central model-tool loop, AgentRun/AgentStep ledger, Tool Registry, central permission policy or Context Builder | new architecture requirement |
| Product Research / Product Definition workflow | **NOT GENERALIZED** | currently an intentional independent human+AI segment | design explicit entity/gate before automating |
| General product builder | **NOT IMPLEMENTED AS ONE SYSTEM** | product creation depends on product type and separate execution workflows | keep type-specific builds behind approved definition |
| Automatic Xiaohongshu publishing/engagement | **INTENTIONALLY NOT IMPLEMENTED** | V1 only produces pending-publication packages | remain denied unless future explicit business decision changes it |

## Current real Phase A checkpoint

Current baseline facts from the current handoff:

- 71 ranked candidates processed;
- 2 `in_scope`;
- 31 `out_of_scope_physical`;
- 36 `needs_human`;
- 2 `collection_failed`;
- 0 waiting;
- current analysis `5859e6c9-0485-451b-9152-05896c0d037b` succeeded;
- current Opportunity `caed5776-ef38-4a0a-90fe-57ae2291583e` is `warming_candidate + approved`;
- approval authorizes independent product research only.

The successful provider call used Bailian `deepseek-v4-flash`, returned in approximately 45.8 seconds and reported:

- prompt tokens: `243,276`;
- completion tokens: `4,984`;
- prompt version: `tutorial-demand-radar-specific-demand-v2`.

That token count is not a theoretical concern; it is a measured production-like workload and should be one of the fixed replay benchmarks for next-generation context work.

## Strongest reusable assets

### 1. Truth boundary

The combination of SQLite records, artifact identities, source identities, manifests, SHA-256 and immutable evidence snapshots is stronger than a normal LLM workflow project. A new workbench should integrate around this boundary rather than replacing it with conversational memory.

### 2. Failure semantics

The system distinguishes:

- failed;
- needs human;
- partial/incomplete evidence;
- historical but ineligible evidence;
- successful current evidence.

This is critical. Future Agents must consume these states instead of flattening everything into “tool succeeded/failed.”

### 3. Real UAT corpus

The UAT history contains concrete failures involving:

- Android disconnects;
- selector/layout changes;
- product-grid ordering;
- short shops/natural end;
- missing third product;
- stale local backend process;
- provider timeouts;
- semantic false-positive Opportunity logic;
- evidence grounding failures;
- Windows newline/hash mismatch;
- uncertain DB/filesystem commits.

These failures are valuable regression assets for a rewrite. A cleaner new architecture is not acceptable if it reintroduces one of these already-solved failure classes.

## Capabilities that should not be overclaimed

1. A green controlled adapter test is not proof of live Xiaohongshu/Qianfan/Android behavior.
2. Existing content code is not proof that the tutorial-equivalent content business loop has passed end-to-end real UAT.
3. A Product row is not an approved Product Definition.
4. An approved Opportunity is not permission to build a product.
5. Historical evidence remains useful for audit but cannot be silently promoted to current eligible evidence.
6. The current repository has no generalized autonomous Agent Runtime.

## Next use of this document

Open-source candidates must be scored partly on whether they can host these existing capabilities without weakening them. A candidate that provides a beautiful Agent UI but cannot preserve durable evidence, needs-human states, permission gates and replayable tool results is a poor fit regardless of popularity.
