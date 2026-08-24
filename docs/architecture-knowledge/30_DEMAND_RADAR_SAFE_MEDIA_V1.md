# 30 — Demand Radar Safe Media V1

Status: **Stage 7.2.1 implementation complete locally; formal Draft PR / GitHub CI acceptance pending**

Date: 2026-08-24

## 1. Purpose

Stage 7.1/7.2 established a tutorial-aligned Demand Radar, but the business surface still showed only an image-evidence count. The tutorial standard requires the operator/AI to actually inspect product images before treating an account/direction as understood.

This slice closes that narrow A-stage gap without entering Product Definition / Phase B.

The requirement is:

```text
Opportunity
  -> representative supporting product
  -> trusted product detail image
  -> safe browser preview
  -> click to inspect the full evidence image
```

The browser must never receive an arbitrary local filesystem path and must not become a generic Artifact file browser.

## 2. Authority chain

A media request is authorized only through already-durable business evidence:

```text
OpportunityRecord.supporting_products_json
  -> supporting product evidence_id = artifact:<shop-result-id>
  -> AnalysisRecord.evidence_snapshot_json (immutable accepted snapshot)
  -> trusted_shop_result
  -> exact product raw_evidence.detail_screen
  -> detail_screen.artifacts
  -> same physical shop Job android_screenshot Artifact
  -> transition + SHA256 identity match
  -> contained runtime-relative regular file
  -> payload SHA256 recheck
  -> bounded Pillow image validation
  -> HTTP image bytes
```

This deliberately does **not** authorize media by `artifact_id` alone.

Knowing that another Artifact id exists is insufficient. An id outside the current Opportunity's immutable supporting-product evidence scope returns 404, so the endpoint does not disclose whether that foreign Artifact exists.

## 3. Why only `detail_screen` is exposed

The Android worker persists many screenshots during a physical collection:

- shop screen;
- before-click screen;
- product detail screen;
- share screen;
- return-to-shop screen;
- later scroll/transition screens.

Those are all useful for physical-worker audit, but they are not interchangeable product evidence.

`ShopCollectionService._materialize_bounded_sample_evidence()` already defines the stronger contract for a sampled product: it requires the product's exact `raw_evidence.detail_screen`, verifies that it maps to one same-Job screenshot Artifact, checks transition and SHA metadata, verifies the file hash, then copies that screenshot into the bounded sample product manifest.

Therefore Safe Media V1 exposes only this exact `detail_screen` binding. It does not use a loose `product_N_*` filename match and it does not show share/return/operational screenshots as if they were the product detail image.

## 4. Backend contract

Business projection now adds an opaque list:

```text
representative_product.image_artifact_ids: number[]
```

No Artifact path is added to the Demand Radar JSON.

New endpoint:

```text
GET /api/v1/business/demand-radar/{opportunity_id}/media/{artifact_id}
```

### Success requirements

The server requires all of the following:

1. Opportunity exists and has an accepted Analysis relationship.
2. Requested Artifact id is derived from one of that Opportunity's `supporting_products_json` entries.
3. The supporting product key matches immutable Analysis snapshot truth:
   - evidence id;
   - product id;
   - source URL.
4. Evidence id resolves to a persisted `shop_collection_result` Artifact produced by `android_shop_worker_v1`.
5. The immutable shop result explicitly contains the product's `detail_screen` evidence.
6. `detail_screen.artifacts` includes the candidate screenshot path.
7. That path is also in the immutable shop result's `evidence_artifacts` list.
8. A same-Job `android_screenshot` Artifact exists for that exact path.
9. `detail_screen.transition` equals Artifact metadata transition.
10. `detail_screen.screenshot_sha256` equals Artifact metadata SHA256.
11. The current file is a contained runtime-relative regular file; no symlink/junction/path escape is allowed.
12. Current bytes still hash to the persisted SHA256.
13. Image bytes are bounded and decode as one of PNG/JPEG/WEBP.
14. Dimensions are non-zero, <= 24M pixels, and animated images are rejected.

### HTTP behavior

```text
allowed + intact -> 200 image/*
outside Opportunity scope / missing -> 404
allowed but integrity validation fails -> 409
```

Success headers include:

```text
Cache-Control: private, no-store
Content-Disposition: inline
X-Content-Type-Options: nosniff
ETag: "sha256-..."
```

Error text is intentionally generic and does not return the local Artifact path.

## 5. Frontend behavior

`/opportunities` now behaves as follows for each representative product:

```text
has safe image_artifact_ids
  -> show real detail-image thumbnails
  -> each thumbnail is clickable
  -> click opens an in-workbench large-image dialog
  -> operator can inspect the actual product evidence

no safe binding
  -> keep evidence-count placeholder
  -> explicitly say the current record has no verifiable safe-preview binding
```

This is fail-closed. `image_evidence_count > 0` does not force an image onto the screen if the stronger `detail_screen` Artifact relationship cannot be proven.

The page still shows the original product link, title, price and sold signal alongside the image.

## 6. Real historical-data validation

A read-only audit first checked existing runtimes with SQLite URI `mode=ro`. No historical database was initialized or migrated for this search.

A complete real chain exists in:

```text
phase-a-uat-20260820-03
```

Observed durable state:

```text
analyses: 10
opportunities: 3
job_artifacts: 1612
jobs: 224
successful Opportunity rows: 3
```

The three accepted Analysis snapshots contain real shop evidence and `detail_screen` records.

For code-level UAT, the historical SQLite database was copied into untracked `.local/` and any initialization/migration was confined to that copy. The service read the original runtime evidence files read-only.

Result:

```text
Demand Radar directions: 3
representative product positions inspected: 9
safe detail-screen Artifact bindings resolved: 7
safe image payloads re-read and SHA-verified: 7/7
media type: image/png for all 7
payload sizes: approximately 0.85 MB to 1.83 MB
```

Two historical representative products had positive `image_evidence_count` but did not satisfy the stricter current `detail_screen` binding. They correctly resolved to no preview rather than being guessed or backfilled.

### Direct visual spot-check

One real resolved image, Artifact `1605`, was opened directly through AgentDock image viewing after the binding check.

It is a genuine Xiaohongshu product detail screen for:

```text
七宗罪分布测试(全新) + 七美德测试（全新）
```

The visible page includes:

- a seven-deadly-sins radar graphic;
- price `¥5.99` and displayed arrival price `¥0.99`;
- sold signal `1.4万+`;
- `付款后自动发货`;
- the product title and product-detail purchase UI.

This proves the new binding is not accidentally selecting a share/return/transition screenshot.

The historical database itself was not mutated by this validation.

## 7. Local automated acceptance

Backend:

```text
python -m pytest backend/tests/business/test_business_workbench.py -q
5 passed
```

The tests prove:

- Opportunity projection emits opaque image Artifact ids without local paths;
- an allowed detail image returns exact bytes;
- same-database/same-Job but non-authorized screenshot id returns 404;
- digest mutation after analysis returns 409;
- safe response headers are present.

Frontend:

```text
DemandRadarPage focused tests: 6/6 PASS
complete frontend suite: 82/82 PASS
production build: PASS
```

Playwright:

```text
desktop Demand Radar: PASS
mobile Demand Radar: PASS
2/2 PASS
```

The browser tests include real `<img>` rendering through the new URL shape, opening the evidence lightbox and closing it, while preserving the desktop/mobile no-horizontal-overflow contract.

Broader local backend run:

```text
backend/tests/business + backend/tests/agent_runtime
151 PASS + 1 known Windows 1ms timing flake in the combined run
```

The known test was immediately rerun alone:

```text
test_cooperative_tool_deadline_is_categorized_and_persisted
1/1 PASS
```

No production timeout behavior was changed for this unrelated local timing flake.

Also passed:

```text
python -m compileall backend/app
python tools/scan_release_boundaries.py --root .
git diff --check
```

## 8. Security/non-goals

This slice does **not**:

- expose arbitrary `JobArtifactRecord.path` values;
- provide a generic `/artifacts/{id}/file` endpoint;
- let the browser browse runtime directories;
- accept a filesystem path from the client;
- expose XML/hierarchy or arbitrary JSON artifacts;
- expose every Android operational screenshot;
- fetch arbitrary remote URLs on behalf of the browser;
- replay Android/XHS collection;
- modify the immutable Analysis snapshot;
- retroactively rewrite old Opportunity evidence;
- claim that every historical `image_evidence_count` has a safe preview;
- implement original commercial image downloading beyond the already-proven detail-screen evidence contract;
- enter Product Definition / Phase B.

If a later A-stage requirement needs every original product carousel/detail asset rather than the currently trusted detail screenshot, that must be a separate collection/evidence contract with its own N/N and SHA acceptance. Safe Media V1 must not invent those files.

## 9. Exit criterion

Stage 7.2.1 is locally complete when:

```text
real Opportunity
  -> immutable product evidence
  -> trusted detail_screen Artifact id
  -> safe HTTP image bytes
  -> Demand Radar thumbnail
  -> click-to-inspect large image
```

is proven without exposing local paths or weakening existing Evidence/Job authority.

Formal completion still requires the stacked Draft PR and latest dedicated/cumulative GitHub checks to pass. Do not enter Phase B merely because the local slice is green.
