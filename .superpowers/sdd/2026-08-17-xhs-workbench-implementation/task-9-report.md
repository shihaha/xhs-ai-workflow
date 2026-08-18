# Task 9 report: complete fact-backed dashboard flows

## Scope delivered

- Added operator routes for `/radar`, `/accounts/<user-id>`, `/opportunities`, and `/content`, while retaining `/status` and `/jobs`.
- Added an operator-facing JSON import for the existing persisted rank-snapshot API. It accepts real captured input and is explicitly labelled as import, not Playwright collection.
- Added a typed API client for persisted rankings, accounts, devices, jobs, analysis evidence/results, opportunities, products/materials, content items/reviews/regeneration, and content packages.
- Added the bounded workflow: persisted ranking facts → account/device N/N job → grounded account/opportunity analysis → evidence-backed opportunity → product/materials → model draft → reject/regenerate or approve → local pending-publication ZIP.
- Every action reloads its persisted facts after the API returns. State-changing controls are shown only for the persisted content state (`review`, `rejected`, `approved`); exported items expose no stale review actions.
- Added real loading, empty, API error, device unavailable, `needs_human`, failed package, available package, and retry states. No production demo seed, fake progress, timer, or automatic publish action was added.
- Reads all persisted ranking/account pages in bounded 100-row API pages; account detail no longer misreports records beyond the first page as missing.
- N/N job cards project persisted missing references/reasons from trusted artifact metadata, link to the durable job evidence section, and preserve prior jobs while directing the operator to resolve the human gate and enqueue a new run.
- Account analysis history now exposes persisted claims, input evidence IDs, safe error detail and a concrete next action for failed/`needs_human` results.
- Documented two existing backend boundaries honestly: there is no HTTP route that starts Qianfan Playwright collection, and shop N/N detail is currently exposed as a trusted job artifact path rather than a file-read/download API.

## TDD evidence

- Initial page RED: four new page suites failed import resolution because the pages did not exist.
- Initial route RED: the application did not expose the workflow routes/navigation.
- Persisted-refresh RED: three tests proved that account, opportunity, and content mutations left the page on pre-action facts.
- Export-state RED: an exported item still offered reject/export controls.
- Vitest isolation RED: after Playwright was added, the unit runner incorrectly collected the E2E spec; Vite/Vitest now excludes `e2e/**` using the upstream default exclusions plus the explicit E2E boundary.

## Controlled E2E

`frontend/e2e/empty-to-package.spec.ts` starts a test-only FastAPI app with:

- a fresh `TemporaryDirectory` and SQLite database per backend process;
- controlled deterministic Qianfan, model and Android adapters;
- runtime-contained source/image/shop evidence with a valid N/N manifest;
- no production database, live credentials, external platform writes, or pre-seeded production business records.

The browser starts the exact automatic eight-scope Qianfan route, waits for persisted 8/8 success, opens the account produced by those snapshots, queues a shop job that reaches persisted `succeeded` 1/1, selects its trusted artifact, creates a grounded opportunity/product, adds managed materials, generates/reviews/approves content, exports a ready/available ZIP, verifies no browser console/page errors, and checks a 360px responsive view. Every repeated run uses unique downstream business identities and filters its own persisted entities; the second material write is acknowledged before reload.

## Verification

- `npm test -- --run`: 8 files, 27 tests passed.
- `npm run build`: TypeScript check and Vite build passed; 35 modules transformed.
- `npm run test:e2e`: 1 controlled fresh-database workflow passed on the final standalone run.
- `npx playwright test --repeat-each=5`: 5/5 passed after review-driven race/isolation hardening.
- `npm audit`: 0 vulnerabilities.
- Screenshot was visually inspected from the controlled E2E output; final persisted item state was `exported`, the package was `available`, and stale review controls were absent.

## Not run / not claimed

- Qianfan authenticated live collection: `not_run` (no authenticated session, configured browser/profile, or verified production selectors).
- Android real-device collection: `not_run: device unavailable` for the live environment; the E2E device is explicitly controlled test infrastructure.
- Bailian live generation: `not_run: BAILIAN_API_KEY unavailable`; the E2E model is deterministic test infrastructure.
- Seven-day real UAT: `not_run`.
- `agent-browser` CLI was not installed; Playwright performed the browser execution, screenshots, responsive check, and console/page-error assertion instead.

## Independent review fix round 1

The first independent review reported 0 Critical and 5 Important. Four frontend-owned findings were closed with RED/GREEN coverage: E2E acknowledgement/isolation, complete account/snapshot pagination, N/N missing-item and human-recovery presentation, and complete account-analysis result/error presentation. Radar now exposes the existing snapshot POST through an honest import form and the E2E begins there.

The remaining Important is a confirmed backend orchestration gap: no HTTP route starts the already implemented Qianfan Playwright adapter. The parent coordinator separately classified this as an older backend acceptance omission and will handle it as a controlled backend supplement. This Task 9 frontend commit does not claim that importing captured JSON starts live collection and must not be labelled a complete live-collection UI until that route is delivered.

Independent re-review found no remaining/new Critical or Important within the frontend scope. One Minor is deferred: state-changing buttons do not share a uniform in-flight disable, so very fast repeated clicks can enqueue duplicate requests before the backend responds. Backend CAS/state validation remains authoritative, but a UI pending fence should be added during the controlled backend supplement or Task 10 hardening.

## Controlled Qianfan frontend supplement

The later backend supplement added `POST /api/v1/radar/qianfan-collections`. Radar now calls that exact endpoint with only `expected_count_per_scope`, then filters `/jobs` by the eight returned job IDs. It displays the collection identifier, the eight-job batch (the API does not return a separate batch identifier), each persisted scope state/count/error, runtime health, and the selector profile recorded in each job. It never derives a percentage or calls 7/8 complete. Polling is bounded to ten checks and cancelled during cleanup; operators can manually refresh after the bound.

The captured JSON path remains available as a clearly separate manual evidence-import workflow. It is not described as Playwright collection. The previously recorded Minor is closed: Radar, account/device, analysis, product, material, content generation, review, regeneration and export mutations are protected by an immediate single-flight guard and all corresponding controls are disabled while work is in flight.

Fresh controlled verification: Vitest 8 files / 34 tests passed; TypeScript and Vite production build passed; Playwright fresh temporary SQLite E2E passed 1/1; `--repeat-each=5` passed 5/5. The browser E2E installs deterministic Qianfan, device and model adapters and now uses the account produced by that controlled automatic collection for the complete downstream flow; it is not a live provider/device claim. Live Qianfan, Bailian, Android and seven-day UAT remain `not_run`.

The first supplement review found 0 Critical, 2 Important and 2 Minor. The browser-executable health key now matches the backend `checks.browser` contract and explicitly does not claim Playwright/profile readiness; scheduled `/jobs` refresh failures are caught, displayed as potentially stale facts, and remain manually retryable; the E2E no longer switches to a manually imported account; and deferred-promise double-click coverage now protects Radar, Account, Opportunities and Content Studio single-flight behavior. Re-review raised one wording Minor, which was fixed; final independent re-review is CLEAN with 0 Critical, 0 Important and 0 Minor.
