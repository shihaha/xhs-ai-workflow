# Implementation status

Status date: 2026-08-21

## Short-shop evidence and XML byte-hash correction

The current evidence contract uses the first three distinct products when at
least three exist. It accepts one or two only when the same Android run reaches
an explicit natural end, has no non-duplicate rejection and successfully binds
every available item. The service derives the exact target from that trusted
adapter result; generic partial collections remain fail-closed.

Android hierarchy artifacts are now written from the same UTF-8 byte sequence
used to calculate their SHA-256, removing Windows newline conversion from new
evidence. Historical artifacts are unchanged.

New live CDP job `7c11ae26…` succeeded at latest 10/10. Final live Android job
`fb293aa0…` truthfully persisted two observed products plus the natural-end
proof and succeeded at exact available 2/2 with 23 artifacts, two distinct
discoveries and a complete bounded sample. All 22 SHA-bearing files match,
including ten XML files; manifest/collection hashes and restart readback also
match. The analysis trust reader accepts this shape only with the persisted
natural-end proof and exact matching counts/files. Clean replacement job
`8c369a80…` succeeded for the other account at 3/3 with 33/33 SHA matches.

Unified real analysis `26979db8…` then consumed two clean shop facts and twenty
latest-note facts. It succeeded with no specific shared demand and zero
Opportunities; restart readback matched. The current Phase A two-account
decision loop is complete without a candidate. Historical mismatches remain
immutable audit history, and Phase B is unstarted.

## Connected-phone ranked continuation

The previously blocked live path resumed after ADB reported one authorized
device and Xiaohongshu foreground readiness. Candidate advancement remained
serialized and score-ordered through position 61. The live funnel now contains
2 in-scope, 31 physical, 26 needs-human, 2 failed and 10 waiting candidates.

The second in-scope account is real and natural, not targeted: Android gate
`151a887f…` succeeded and restart-read correctly. Its downstream evidence is
not complete. XHS profile/latest-10 job `d3a4d2d1…` is `cdp_unavailable`, and
formal shop evidence job `77fedee2…` is a strict 2/3 partial with two persisted
discoveries, `expected_products_missing`, `sample_complete=false` and no fourth
replacement. Consequently the account cannot yet enter specific-demand
analysis.

No files are missing across the three historical jobs. Twenty XML artifacts fail their
saved/current SHA comparison because of the already documented Windows newline
expansion; 27 other SHA-bearing artifacts match. No production code changed in
this historical continuation. The newer short-shop verification and unified
analysis above supersede its blocker statement; the current Phase A
two-account decision loop is complete without a candidate, and Phase B is
unstarted.

## Ranked candidate business-scope funnel

The Phase A flow now keeps eight boundaries explicit: original Qianfan scoring;
Top-N priority rather than a hard rank-20 stop; evidence-bound low-cost scope
prescreen; authoritative Android scope; score-ordered account replacement;
the unchanged first-three-products shop sample; unified specific-demand
analysis over qualified accounts; and human Opportunity review.

The new Radar service reads persisted ranking/profile/latest-10 facts, writes an
immutable job artifact with cited evidence and hashes, and returns only
`likely_digital`, `clearly_physical` or fail-open `uncertain`. It never upgrades
prescreen evidence to shop evidence. The next-candidate endpoint accepts a date
and optional device only; it has no category or matching input and queues the
existing exact-three Android preflight for the next eligible score position.
The Radar UI presents Chinese operator labels and displays prescreen and final
Android results separately.

General RED/GREEN coverage includes clear physical evidence, ambiguity,
classifier failure, non-eligibility of likely-digital facts for analysis,
rank-21 replacement, non-targeted ordering, active-job serialization, and
Android-final precedence. Current verification is Radar 112 passed, focused
shop 33 passed, analysis 178 passed/1 skipped, frontend 65 passed, and a passing
production build.

The whole backend tree additionally reported 1498 passed, 3 skipped and 5
failures in unchanged media and legacy/shop-scope expectations. Focused tests
for every modified production path remain green; the unrelated failures were
not repaired in this bounded Phase A increment.

Real UAT persisted and hash-verified all 71 candidates (4 physical, 2 likely
digital, 65 uncertain) and restart-read the same 71. The first two new ordered
continuations failed as `device_disconnected`; ADB listed no attached device.
Therefore the code and low-cost live chain are implemented, while real Android
continuation, additional qualified-account evidence and the subsequent unified
analysis remain incomplete. Phase B is unstarted.

## Specific-demand semantic gate and review explanation

The real `37d7fca6…` output proved a missing business contract: it produced two
different product clusters but promoted their shared marketing method into a
single Opportunity. Cross-account analysis now requires a cited demand profile
for each account and a cited conclusion stating the common demand,
commonalities, key differences and rationale. Shared sales channels and broad
needs such as saving money are insufficient. If no specific shared demand
exists, the structured result must use `common_demand=null` and
`opportunities=[]`; the service rejects a contradictory Opportunity while
retaining all previous grounding checks.

The opportunity page loads persisted Analysis records and explains what each
account proves, the proposed common demand, similarities, differences,
rationale and supporting evidence. Historical candidates without the new
explanation are labelled as incomplete for approval rather than rewritten.

The bounded independent review also closed two Important gaps: a cross-account
conclusion must itself cite every requested account, and approval now requires
a positive specific-demand conclusion. Legacy or negative candidates remain
rejectable but cannot be approved, including through direct API use.

The analysis backend regression is `178 passed, 1 skipped`; frontend tests are
`63 passed`; the frontend build passes. Real analysis `3322dda1…` reused the
same two accounts and 22 evidence IDs, succeeded with the v2 prompt, and
persisted a negative shared-demand conclusion with zero Opportunities. Its
trust fingerprint and restart readback match. The historical analysis and
pending Opportunity remain unchanged. Phase B remains unstarted.

## Grounded two-account candidate live result

The first real model response passed strict schema parsing but failed the
service-side evidence-grounding rules. Because rejected model output is not
persisted, the exact offending field is unavailable. A RED test demonstrated
that the prompt did not state the validator's exact account-coverage,
evidence-ownership and support-citation requirements. The minimal GREEN change
now exposes those existing rules without weakening any check; the analysis
regression passes `70/70`.

One authorized real call reused the same two accounts and 22 trusted facts.
Analysis `37d7fca6…` succeeded and durably created one
`warming_candidate + pending_review`. Restart reads confirm support from both
accounts, two trusted shop facts and twenty trusted note facts, with all support
IDs included in the candidate citations. The candidate remains unreviewed. No
Phase B product, content or ZIP work has started. The demand-validation
milestone is proven, while full UAT remains open for human review and the known
historical XML current-byte hash limitation.

## Approved evidence-sample live result

Large in-scope shops now have one approved non-test collection mode:
`evidence_sample` takes the first three distinct products in default shop order,
forbids selection/skipping/fourth-product substitution, requires a trusted
`in_scope` preflight and exact detail/source/image/manifest/hash/SQLite
bindings, and persists `sample_complete=true` with `shop_complete=false`.
Unlike `bounded_sample/test_override`, an exact evidence sample may support
Phase A cross-account demand validation.

Real job `84f11a0d…` succeeded at 3/3 with zero missing and 34 artifacts. A
restart trust read accepted the result. The first real two-account analysis then
used two trusted shop facts and twenty trusted note facts, but analysis
`28856ff2…` failed closed as `evidence_grounding_failed`; zero Opportunity,
`warming_candidate` or `pending_review` rows were created and no automatic
retry was run. Phase A real UAT remains not passed; Phase B remains unstarted.

## Dynamic-card live verification result

The Android adapter candidate now uses three bounded behaviors required by the
latest real failure: two consecutive stable product/bounds samples after a
swipe, fresh same-signature relocation immediately before clicking, and one
fresh same-target retry when the detail-page postcondition is absent. A reliable
current clickable-card ancestor supplies the safe point when the hierarchy
contains one; otherwise the current visible title bounds remain the fallback.

Focused tests pass `5/5`. The complete shop collection file is `40 passed,
1 failed`; the remaining failure is the known ShopCollectionService scope
expectation outside this Android path. The latest authorized real-phone job
then succeeded at `3/3` with no rejection, 15 screenshots, 15 hierarchies and
three discovery JSON files. Restart read preserved the succeeded state,
progress, 33 artifacts, discovery order and three distinct identities.

Screenshot and discovery JSON byte hashes match their metadata. XML metadata
still hashes the original hierarchy rather than the Windows-expanded on-disk
bytes; that known evidence-chain limitation remains explicit. The older section
below describes the earlier failed validation and is retained as history. Phase
A overall remains not passed, and Phase B has not started.

## Bounded Android shop ordering fix

The retained Phase A failure was caused by a contract mismatch inside the
Android adapter: two-column shop XML arrives in column-major DOM order, while
viewport overlap and click traversal require visual row order. The parser now
performs one final `(center_y, center_x)` sort. A real-coordinate column-major
fixture failed before this change and passes after it; the earlier overlapping
viewport behavior remains covered.

This is not a live-complete claim. The historical job remains `needs_human`
with two discoveries. The only new validation job failed before device
interaction because its one-off invocation supplied an unsupported keyword,
so no third real discovery exists. Restart reads preserve both job states and
the two historical discoveries. Screenshot and discovery JSON hashes match;
historical XML current-byte hashes do not match their pre-write hierarchy
metadata after Windows newline expansion. Phase A UAT therefore remains not
passed, and no Phase B capability was started.

## Active business phase

Phase A is the only active implementation scope: cross-account demand validation and human opportunity review. Existing collection, evidence, recovery and media infrastructure is retained. Product construction, content research, templates, Skills, content generation and ZIP work are not being extended in this phase.

The approved Phase A contract distinguishes single-account observation signals from market opportunities. Cross-account analysis requires at least two different accounts, complete trusted shop evidence and trusted account notes for every account. The server—not the model or operator—computes `warming_candidate` for two accounts and `validated_candidate` for three or more. Valid model clusters enter `pending_review`; only a human `approved` result may cross the product gate, while `rejected` history remains durable.

## Software scope

Tasks 1–9 have implemented the local FastAPI/React/SQLite workbench, durable
jobs/evidence, normalized adapters, Qianfan orchestration, Android N/N workflow,
grounded Bailian analysis, reviewed content production, quarantined cleanup, and
the complete operator UI. Task 10 supplies release/recovery/security checks and
Windows runbooks.

The account-note collection extension now adds the production read-only
`xhs-cli` adapter path, reserved account/search jobs, exact profile-plus-note N/N
persistence, hash-bound evidence, account-note analysis grounding, and truthful
Account/Radar operator states. Account notes can ground analysis claims; they do
not replace the existing exact shop N/N gate for opportunity creation.

Account/search polling now allows 60 checks by default. Reaching that bound
releases the page-level single-flight lock without inventing a terminal job
state: the prior job stays visible for audit and can be resumed by its exact
returned ID or superseded by a newly submitted job. Account-note evidence is
selectable and labelled trusted only when the API returns its trust eligibility
flag as true; false entries are disabled and require human verification.

The Bailian media extension adds separate trusted vision and image-generation
configuration, durable reserved media runs, managed generated `output_image`
files, sealed advisory visual assessments, worker/API lifecycle and Content
Studio controls. Requests expose only current revision, image-plan entry and
managed material IDs; operators cannot enter provider endpoints, models, URLs
or filesystem paths. Visual advice cannot approve content or satisfy a human
per-image check.

The controlled Playwright flow begins with a fresh temporary SQLite database and
runs ranking collection through an available ZIP using deterministic test-only
Qianfan/device/model adapters and the production XHS adapter fed pinned-CLI JSON
shapes. It creates a unique ranked account on every
run, starts account-note collection from the UI through the production Task 3
job/artifact/database service, selects the newly persisted `account-note:*`
identity for analysis, and does not pre-seed account notes or substitute a
manual-import account. A fresh production database inserts no demo business
records. The cleanup API is read-only; reserved worker jobs reject public
result/log/artifact forgery; paths are runtime-contained; credentials are
environment-only.

## Release hardening decisions

- `xhs-cli` reads require an externally prepared cookie file in an app-owned
  state directory. The child gets an allowlisted environment whose profile and
  cache paths all resolve under that directory; missing state is
  `needs_human/login_required` and no CLI process starts.
- The XHS child uses fixed argv with `shell=False`. Stdout and stderr are bounded
  while the process runs; timeout or overflow terminates/reaps the child and
  yields only a sanitized category. Real top-level list, `userPageData`,
  `userInfo`, nested `noteCard`, profile-stat and xsec shapes are covered.
- Job evidence is fail-safe retained on cancellation, rollback and uncertain
  commit acknowledgement; no job failure path permanently unlinks the only copy.
- Ambiguous source URL literals (empty query/fragment markers and doubled
  leading path slash) are rejected instead of normalized silently.
- Identical analysis digests are explicitly allowed as separately audited
  intentional reruns; no cross-analysis opportunity merge occurs.
- Python requirement is 3.12+; Node minimum is 20.19+ and the verified version
  is pinned to 24.18.0.

## Recovery/security coverage inventory

| Scenario/boundary | Automated evidence |
|---|---|
| Lost login/cookie, captcha, layout timeout | `backend/tests/radar/test_rank_ingestion.py`, `backend/tests/shops/test_shop_collection.py` |
| Device disconnect/cancellation/N-N mismatch | `backend/tests/shops/` |
| Model 429, timeout, network exhaustion, secret redaction | `backend/tests/analysis/test_model_failures.py` |
| Process/job/cleanup restart | `backend/tests/integration/test_recovery_matrix.py`, cleanup worker/service suites |
| Job evidence commit ambiguity | `backend/tests/test_release_hardening.py` |
| Traversal/containment and Windows-equivalent artifact paths | jobs/content hardening and quarantine suites |
| Reserved worker APIs and read-only cleanup API | `backend/tests/test_jobs_api.py`, `backend/tests/content/test_cleanup_api.py` |
| Account/note collection states, polling release/resume and audit history | `frontend/src/pages/AccountPage.test.tsx`, `frontend/src/pages/RadarPage.test.tsx` |
| Account-note trust through fresh empty runtime and ZIP | `backend/tests/analysis/test_account_note_grounding.py`, `frontend/e2e/empty-to-package.spec.ts` |
| Generated PNG through API/worker/DB/file/UI, advisory vision and human-gated ZIP | media backend suites, `frontend/src/pages/ContentStudioPage.test.tsx`, `frontend/e2e/empty-to-package.spec.ts` |
| Guarded Bailian image/vision live gate | `backend/tests/integration/test_bailian_media_live.py` (explicit isolated opt-in passed on 2026-08-19 with real `wan2.6-t2i` and `qwen-vl-max`; default without opt-in remains exact `not_run`) |
| Guarded local XHS CLI contract | `backend/tests/xhs/test_live_cli_contract.py` (isolated externally prepared state, real `status` text shape, JSON `whoami` identity, exact read-only commands; bounded live gate passed on 2026-08-19 for current profile + 3 notes + empty search 0/0; default without opt-in remains `not_run`) |
| Fresh empty runtime through ZIP | recovery-matrix empty-state test and `frontend/e2e/empty-to-package.spec.ts` |

## Not verified live

Authenticated Qianfan live UAT passed on 2026-08-20 in an isolated runtime:
collection `b91a86a6-59c9-470f-978c-d875a45ac664` completed the exact 4×2
scope matrix, with all 8 jobs at 10/10, 8 raw-capture artifacts, 0
credential-like-key hits, and direct SQLite verification of 8 snapshots with
10 submitted/10 persisted items per scope. The initial API filtering script
looked at the wrong `raw_evidence` level; direct database facts are the
authoritative result. Qianfan software uses the supported
`qianfan-note-rank-live-v1` request-bound contract, so only active UI plus a
successful canonical `POST` response (`sortBy`, `noteType=0`, `pageNo=1`,
`pageSize=10`) establishes a scope. The prior `layout_changed` and
`scope_unverified` UAT attempts remain preserved debugging evidence.

A non-empty live XHS keyword search, the ranked-account analysis continuation,
durable post-collection binding of the two Android-observed products, a trusted
ranked-account public-profile read, and the seven-day run remain pending. A real Vivo V2303 / Android 16 device completed a bounded
read-only ranked-account shop pass on 2026-08-20 with exact device counts 2/2
and 16 screen/UI evidence files. Its same-batch detail screenshots plus exact
link/hash manifests passed the strict verifier at 2/2, but the durable job
remains honestly `needs_human/product_evidence_verification_pending` because
the current pre-collection-only verification input cannot bind changing share
short links after collection. Two ranked-account `user-posts` reads each
returned 5 notes while the paired public-profile `user` reads failed, so no
ranked-account facts were persisted. The bounded authenticated local `xhs-cli` gate passed for the current profile, 3
notes and an exact empty search on 2026-08-19. Bailian text was attempted with
real credentials and HTTP 200 but failed strict `AnalysisOutput` validation as
`model_output_invalid`; it is not a live success.
Bailian image generation and visual assessment passed their bounded real-model
gate on 2026-08-19. See `docs/UAT_CHECKLIST.md`. Therefore the honest release label is
**software implemented / awaiting real UAT**.

## Phase A cross-account validation (2026-08-20)

The Phase A software boundary is implemented: single-account reports cannot
create opportunities; cross-account cards must bind complete per-account shop
and note evidence; the server computes two-account `warming_candidate` and
three-plus-account `validated_candidate`; candidates start at `pending_review`;
approval/rejection is human-only and terminal; product creation requires an
approved eligible candidate. The Account, Radar and Opportunities pages expose
that boundary without a Phase B product action.

Controlled backend/frontend/E2E verification passes. The original live
`cli_failed` blocker was diagnosed as an incomplete isolated CLI/private-runtime
state and the known control account was restored. A distinct second account now
has one trusted profile and a bounded latest-10 note sample. Real UAT nevertheless
remains blocked because its Android discovery returned 18 deduplicated product
links only to process memory: the durable job contains 66 screenshots and 66 UI
hierarchies but no `shop_collection_result` and no reusable persisted
`source_url`. This physical-goods account is now `out_of_scope_physical` and is
excluded from real Phase A candidates. The truthful shop sample is `0/3`; no real cross-account analysis,
candidate or review was fabricated. See `docs/PHASE_A_UAT_REPORT.md` and the
Phase A report in `PROJECT_STATUS.md`.

Subsequent software changes make account notes a bounded latest-10 engineering
sample, introduce a default maximum-three-product shop scope preflight, keep
physical stores out of deep collection by default, and strictly separate
`sample_complete` from `shop_complete`. The approved clothing-store E2E override
can only become eligible when an exact 3/3 result binds its result artifact,
`collection.json`, image manifest, SHA values and source-URL ordering. These
contracts do not upgrade the missing historical links into a real success.

## Windows XHS CDP account reader (2026-08-20)

The Windows Phase A runtime now explicitly selects `XhsCdpReadAdapter` for
account profile and bounded homepage-note reads. It attaches only to a trusted localhost CDP origin for an
already-running visible Chrome collection profile. It performs no login and has
no QR fallback. Success requires the requested and final profile route to match,
ten-or-natural-end unique note IDs, and every note owner to match the requested
account. Existing `CollectionRequest`/`CollectionResult`, durable jobs,
artifact/SHA, SQLite snapshot and account-note trust readers are unchanged.

The production control persisted and restart-read one profile plus latest-10 for
the known account. One further ranked account also persisted profile/latest-10,
but its Android scope preflight ended at 2/3 with
`needs_human/selector_changed`; therefore no new qualified account, analysis,
opportunity or review state was created.

### Account delivery-scope gate

Phase A now persists an account-level `unknown | in_scope |
out_of_scope_physical | needs_human` decision with source, timestamp and evidence
references. It does not alter Qianfan scoring. Android preflight persists each
real discovery before re-evaluating scope, stops at the first decisive physical
or digital result, and never treats 1/3 or 2/3 as shop completeness. Deterministic
fulfilment evidence is evaluated first; only ambiguous evidence can use the
existing Bailian text adapter under a strict cited schema.

The production REPor decision was persisted separately from its unchanged
historical failure and was verified after restart. Real ranked continuation found
one digital-delivery preflight, but its full discovery failed closed after two
products. Consequently the implementation gate is available, while the real
Phase A UAT still has no additional qualified account or pending review.
