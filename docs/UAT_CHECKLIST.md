# Live UAT checklist

Automated and controlled-fixture checks cannot pass these gates. Record dates,
operator, job IDs, evidence paths, package ID/hash, and any recovery action.

## 2026-08-21 bounded provider verification and review gate

- [x] Preserve failed analysis `5d4e235a…` unchanged. SQLite records three attempts and three `timeout` categories, no model output, no usage and no Opportunity.
- [x] Run exactly one user-authorized provider request with a 120-second timeout and `max_attempts=1`. Analysis `5859e6c9…` succeeded in 45,797 ms with 243,276 prompt tokens and 4,984 completion tokens.
- [x] Persist the model's specific shared demand, “七宗罪与人格心理测试数字内容”, and exactly one current `warming_candidate + pending_review` using current clean shop and note evidence.
- [x] Read the saved analysis and Opportunity through SQLite and the main backend service. Keep historical failed analyses, ineligible evidence and malformed candidates unchanged.
- [x] Restore the stale port-8000 backend used by the frontend proxy. Verify the opportunity page in a real browser: current title and common demand are present, no load-error panel appears and no console error is emitted.
- [x] Translate the observed system-status and demand-radar error states to Chinese; pass all 65 frontend tests and the production build.
- [ ] Approve or reject the current “七宗罪心理测试数字内容市场机会” after human review. Approval is one-way and must revalidate current evidence trust.
- [x] Keep Phase B unstarted. A Phase A approval does not itself authorize product creation; Phase B requires separate user approval.

## Historical checkpoint: 2026-08-21 ranks 62–71 and formal XML closure

- [x] Process positions 62–71 strictly in original score order. All ten retained `uncertain` prescreen and ended Android `needs_human 0/3`; no targeted pairing or new in-scope account was introduced.
- [x] Persist final funnel counts: 2 in-scope, 31 out-of-scope physical, 36 needs-human, 2 failed and 0 waiting.
- [x] Preserve failed lock-screen evidence job `f987e4f1…`; after proving that external cause, run one clean replacement. Job `8507e249…` succeeded at natural-end 2/2 with 25 artifacts and 24/24 SHA matches.
- [x] Verify both current funnel accounts have trusted profile, latest-10 and clean shop evidence. Position 61 job `fb293aa0…` remains 2/2 with 22/22 SHA-bearing files matching.
- [x] RED/GREEN the missing trust rule: any SHA-bearing artifact mismatch makes its shop result ineligible, and approval revalidates current evidence trust. Historical artifact `401` is now ineligible without being rewritten.
- [x] Run one clean-only unified analysis over 2 accounts and 22 eligible facts. `5d4e235a…` failed closed as `model_retry_exhausted`; do not loop-retry or manufacture an Opportunity.
- [x] Formally classify old newline-expanded XML artifacts as `legacy_historical_audit_limitation`: current exact-byte writer and current clean jobs are verified; historical jobs/artifacts stay immutable and the limitation is no longer a current blocker.
- [x] Keep the pre-gate `73728a7c…` pending candidate as immutable audit history; it uses evidence that is no longer eligible and cannot pass the current approval trust gate.
- [x] At this checkpoint, keep Phase B unstarted because the clean-only unified provider run failed; the later bounded provider result and current review gate are recorded above.

## Historical checkpoint: 2026-08-21 short-shop and CDP verification

- [x] Keep three as the cap for shops with at least three products; accept exactly one or two only when the same Android run proves the natural end and collects every available item without non-duplicate rejection.
- [x] Preserve ordinary 1/3 and 2/3 results as fail-closed; do not skip products or substitute a fourth item.
- [x] RED/GREEN the one-product, two-product, unchanged three-product and exact-written-XML-hash paths.
- [x] Use the dedicated visible Chrome through localhost CDP. New job `7c11ae26…` persisted latest 10/10 and remained readable after restart.
- [x] Run a final real Android evidence job with truthful input. `fb293aa0…` persisted `available_count_observed=2` and `natural_end_reached=true`, then succeeded at proven available 2/2 with two distinct discoveries/sources and `sample_complete=true`, while `shop_complete=false` remains truthful.
- [x] Verify 23/23 files present, 22/22 SHA-bearing artifacts matching current bytes, manifest/collection hashes matching, natural-end XML evidence present, and restart readback consistent.
- [x] Replace the other qualified account's trusted sample without rewriting history. Job `8c369a80…` succeeded at 3/3 with 34 files and 33/33 SHA-bearing artifacts matching current bytes.
- [x] Persist and trust-read the short-shop natural-end flag; reject the same 2/2 shape when that proof is absent.
- [x] Run unified analysis `26979db8…` over two clean shop facts and twenty note facts. It returned no specific shared demand and `opportunities=[]`; restart readback matched.
- [x] Complete the current Phase A two-account decision loop without manufacturing a candidate. Historical XML mismatches remain immutable audit history. No Phase B action has started.

## Historical checkpoint: 2026-08-21 connected-phone continuation

- [x] Restore one authorized ADB device and confirm `com.xingin.xhs` in the foreground; preserve the earlier disconnected jobs unchanged.
- [x] Continue strictly in original score order from position 18 through position 61, with no category input, targeted pairing or concurrent Android jobs.
- [x] Persist final funnel counts: 2 in-scope, 31 physical, 26 needs-human, 2 failed and 10 waiting from 71 candidates.
- [x] Obtain one new natural `in_scope` account at position 61. Preflight `151a887f…` succeeded after 2 representative products and remains restart-readable with 25 artifacts.
- [x] Complete profile/latest-10 for the new account in a new job. `7c11ae26…` succeeded at 10/10; historical `d3a4d2d1…` remains unchanged.
- [x] Complete the formal sample under the current short-shop rule. Final job `fb293aa0…` proved the natural end and succeeded at exact available 2/2; historical `77fedee2…` remains unchanged at partial 2/3.
- [x] Formally close this historical checkpoint's 20 XML mismatches as `legacy_historical_audit_limitation`: newer clean jobs use exact-byte hashing and match current bytes; these immutable old files remain audit evidence and are non-blocking.
- [x] Keep unified specific-demand analysis stopped during this repair. The new account now has trusted profile/notes and opportunity-eligible shop evidence, but no analysis was run in this increment.
- [x] This historical checkpoint was open at the time and is superseded by the completed short-shop and unified-analysis checks above; Phase B remains unstarted.

## 2026-08-21 ranked candidate scope funnel

- [x] Keep the tutorial Qianfan score unchanged and preserve its stable descending order.
- [x] Persist three-state low-cost prescreen results with account, timestamp, reason, cited public facts, raw digests, artifact payload and SHA-256; invalid/failed classification falls open to `uncertain`.
- [x] Prove that `likely_digital` remains Android `unknown`, cannot qualify as analysis evidence, and cannot bypass the existing three-product preflight.
- [x] Prove ordered account replacement beyond rank 20 and prohibit category/direction input or targeted second-account pairing.
- [x] Keep Android final `in_scope | out_of_scope_physical | needs_human` authoritative over prescreen, including after restart.
- [x] Keep the separate shop evidence rule unchanged: first three distinct products in default encounter order; no skip and no fourth replacement.
- [x] Real pool prescreen: 71 candidates yielded 4 clearly physical, 2 likely digital and 65 uncertain. All 71 artifacts have complete cited payloads and valid saved/current SHA bindings; restart returned 71/71.
- [x] Preserve positions 8 and 9 as historical `device_disconnected` jobs, then continue only after the phone became available; no historical job was rewritten.
- [x] Continue through the final score-ordered candidate. No positions 62–71 account became in-scope, so no additional profile/latest-10/shop sample was warranted.
- [x] Run one clean-only specific-demand analysis over the current eligible pool. `5d4e235a…` failed closed as `model_retry_exhausted`; no automatic loop retry or manufactured Opportunity followed.
- [x] This historical funnel checkpoint was open at the time and is superseded by the completed two-account decision loop above; Phase B remains unstarted.

## 2026-08-21 bounded Android click repair

- [x] Prove the scroll failure from retained screenshot/XML: column-major DOM order caused overlap zero and a repeated first-card click.
- [x] Add a real-layout RED test, apply the one-line visual-order sort, and observe GREEN plus the pre-existing overlap regression GREEN.
- [x] Preserve historical job `d21d3e81…` at `needs_human`, `2/3`, `selector_changed`; restart-read the same 22 artifacts and two discoveries.
- [x] Verify the third real product after the dynamic-card repair. Latest authorized job `5319ba52…` succeeded at `3/3`, zero rejected, with three restart-readable discoveries and three distinct source identities.
- [x] Preserve the earlier runner-error job `fe7b58b5…` unchanged at `failed 0/3`; it remains historical audit evidence and was not rewritten.
- [x] Preserve the ten historical XML mismatches unchanged and classify them as non-blocking `legacy_historical_audit_limitation`; current production and eligible evidence are validated separately with exact-byte SHA.
- [x] Preserve the pre-evidence-sample checkpoint where no cross-account analysis, Opportunity, `pending_review`, `warming_candidate`, or Phase B action had run; the later real analysis attempt is recorded below without rewriting this history.

## 2026-08-21 approved evidence sample

- [x] Freeze the general rule at the first three distinct products in default shop order; prohibit selection, skipping and fourth-product replacement.
- [x] Require trusted `in_scope` preflight plus same-job detail screenshots, source identities, manifest/collection hashes and SQLite bindings; keep `sample_complete=true` and `shop_complete=false`.
- [x] Real job `84f11a0d…` succeeded at 3/3 with zero missing, 34 artifacts and restart-readable opportunity-eligible shop evidence.
- [x] Run one real two-account analysis with two trusted shop facts and twenty trusted latest-note facts. Analysis `28856ff2…` failed closed as `evidence_grounding_failed`; no automatic retry was run.
- [x] Expose the existing account-coverage, evidence-ownership and support-citation contract to the model; observe RED before the prompt change and GREEN after it (`70/70` analysis regression).
- [x] Obtain one strictly grounded real analysis result. Analysis `37d7fca6…` created exactly one two-account `warming_candidate + pending_review`, using two trusted shop facts and twenty trusted note facts with all support IDs cited.
- [x] Preserve the raw `37d7fca6…` model output and prove it used the shared marketing method “both promote shop products through notes” as the Opportunity rather than finding one shared concrete demand.
- [x] Add a general specific-demand RED/GREEN contract without shop-specific text: per-account offering/user/motivation/delivery/scenario profiles; explicit commonality/difference/rationale; broad umbrella needs and sales channels are insufficient; no shared demand requires `opportunities=[]`.
- [x] Require the final common-demand conclusion itself to cite every requested account; block approval in both backend and UI unless the parent analysis has a positive specific-demand conclusion. Legacy/negative candidates remain rejectable.
- [x] Re-run the same two accounts and same 22 evidence IDs once. Analysis `3322dda1…` succeeded with `has_specific_shared_demand=false`, `common_demand=null`, and zero output/database Opportunities. Restart readback and the evidence trust fingerprint match.
- [ ] Complete explicit human disposition of the historical candidate if desired. It remains unreviewed and unchanged; the corrected real analysis did not reproduce it. No product, content, ZIP or other Phase B action has started.
- [x] Formally close the historical XML current-byte hash limitation as non-blocking: exact-written-byte production hashing is verified, current eligible shop jobs match all SHA-bearing files, and old artifacts remain immutable audit history.

## Prerequisites

- [x] Authenticated current Qianfan browser profile supplied and health checked.
- [x] Xiaohongshu login supplied without storing credentials in Git.
- [x] Trusted local `xhs-cli` state is prepared outside the application under an isolated directory inside the configured runtime. Set `XHS_LIVE_STATE_DIR` to that directory; do not paste Cookie, token or password into HTTP, tests, logs or this checklist.
- [x] One Android phone connected; ADB/device/app state reports actual availability.
- [x] Bailian API key supplied through environment only; the configured image and vision model calls succeeded in the bounded media gate.
- [x] Explicit `BAILIAN_MEDIA_LIVE_TEST=1`, `XHS_BAILIAN_IMAGE_MODEL` and `XHS_BAILIAN_VISION_MODEL` were supplied only to the one-off test process. The key was not pasted into chat, HTTP payloads or logs.
- [ ] Fresh runtime/database selected; all business lists are initially empty.

## Real end-to-end gate

- [x] Run all eight Qianfan scopes with `expected_count_per_scope=10`; require persisted 8/8 scope facts, canonical `pageNo=1,pageSize=10` raw evidence and no fabricated percentage. Do not count page-size-one helper responses.
- [x] Select a real account and collect its store on the real phone.
- [ ] From that ranked account's UI, run read-only account collection and verify one public profile plus exact note N/N, source links, reserved job, raw artifact hash and database rows agree.
- [ ] Run one public-note keyword search and verify expected N, returned N, source links and the hash-bound search artifact agree.
- [ ] If account/search polling reaches its UI bound, confirm the old non-terminal job remains visible, the new-job action is released, and “continue refreshing” reads only that job's returned ID.
- [ ] Confirm any `account-note:*` entry marked ineligible is labelled stale/untrusted, disabled for analysis selection and retained for human audit.
- [ ] Select the new account's canonical `account-note:*` IDs together with its trusted shop evidence for analysis; confirm notes enrich claims but do not replace the exact shop N/N opportunity gate.
- [ ] Confirm declared N, discovered N, verified N, missing list and image manifests agree.
- [ ] Run a single-account report and confirm it is displayed only as an observation signal and creates no opportunity row.
- [ ] Collect a second independent ranked account with exact profile, note and shop evidence.
- [x] Run a two-account demand cluster and require every candidate to cite trusted shop and note evidence from both accounts.
- [x] Confirm a valid two-account candidate is `warming_candidate + pending_review` and that the evidence count is server-derived.
- [ ] Approve or reject the candidate; confirm rejection retains history and an unapproved candidate cannot create a product.
- [ ] Phase B–E product/content/ZIP UAT is deferred until the user separately approves those phases.
- [ ] From a current review/approved revision, generate one managed image, verify its run/job/request ID/usage/database/file/hash facts, request one advisory visual assessment, and confirm the advice did not auto-approve any human visual check.
- [ ] Compare ZIP manifest, text, images, sources, reviews, DB record, size and SHA-256.

## Seven-day controlled run

- [ ] Day 1 baseline successful closed loop.
- [ ] Cookie expiry/login loss is detected and recovered without false success.
- [ ] Captcha requires manual takeover and is never bypassed.
- [ ] Selector/layout change becomes `needs_human` with evidence.
- [ ] Device disconnect becomes a truthful non-success and recovers by a new run.
- [ ] Model 429/network/timeout exhausts bounded retries without leaking secrets.
- [ ] Process restart changes abandoned work to a visible recovery state.
- [ ] Cleanup quarantine survives restart and respects the 24-hour grace period.
- [ ] Each day: reconcile page state, DB rows, logs, evidence files and package hashes.

## Current execution status (2026-08-20)

- Qianfan authenticated live collection: **passed (2026-08-20)** — isolated runtime `qianfan-live-uat-20260820-002938`, collection `b91a86a6-59c9-470f-978c-d875a45ac664`. All 8 returned jobs succeeded at 10/10; 8 raw-capture artifacts exist; credential-like key scan found 0 hits. Direct SQLite verification (authoritative) found exactly 8 snapshots for 2026-08-20, the exact four-board by two-dimension matrix, and `submitted_count=10` plus `item_count=10` for every scope. An initial API filtering script inspected the wrong `raw_evidence` level and is not used as the verdict. Earlier `75ca2ef5…` layout and `e6f2ffef…` scope failures remain historical debugging evidence only.
- Xiaohongshu account/note bounded live gate: **passed (2026-08-19)** — the isolated opt-in run completed `1 passed in 89.83s`. It verified the authenticated current-account profile, exactly 3 public notes, the reserved job/raw artifact/database path, and an exact empty keyword-search result (0/0), using only fixed `status`, `whoami`, `user`, `user-posts` and `search` reads. The real `user-posts` response used nested page slots and was normalized without treating empty slots as notes. Credentials remained outside argv, logs and project files. A non-empty live search and the ranked-account UI-to-analysis workflow remain pending and are not implied by this bounded pass.
- Android real-device collection: **bounded device pass and same-batch product verification completed; durable binding pending (2026-08-20)** — Vivo V2303 / Android 16 was authorized over ADB and the authenticated Xiaohongshu app was used read-only. Runtime `android-live-uat-20260820-09`, job `4689ae2b-5021-408f-affe-45ed889688de`, declared/discovered/collected `2/2/2`, with 16 screenshot/UI-hierarchy artifacts plus one durable result artifact. The two genuine product-detail screenshots were visually checked, copied into a contained same-batch verification directory with exact link/hash manifests, and `verify_shop_collection()` returned expected/discovered/verified/missing `2/2/2/0`, `complete=true`. The original durable job remains truthfully `needs_human/product_evidence_verification_pending`: the current workflow accepts a verification directory only before phone collection, while each Xiaohongshu share creates a different short link, so it cannot bind this post-collection evidence back to the job without a new reviewed workflow. Earlier runs retain the real Android 16 clipboard/API compatibility failures and the deliberate 2-observed-vs-1-declared overflow rejection as debugging evidence.
- Ranked-account profile/note continuation: **control restored; one second-account sample passed (2026-08-20)** — the original `cli_failed` was traced to an incomplete isolated CLI/private-runtime state rather than candidate ordering. `real-account-A` was successfully re-read through the trusted path, and one distinct candidate persisted one profile plus a bounded latest-10 note sample. The older five candidate failures and the failed control remain unchanged audit history.
- Bailian text live contract: **failed (2026-08-19)** — the configured text model and Key reached Bailian successfully with HTTP 200, but the returned JSON did not satisfy strict `AnalysisOutput` and was rejected as `model_output_invalid` after 7.796 seconds. The production adapter now sends the exact Pydantic JSON Schema and JSON-only/key-preservation instruction; focused contract tests pass, but the real provider output remains non-conformant. No model output, Key or full provider payload was persisted in this record, and no successful analysis fact is claimed.
- Bailian image/vision live gate: **passed (2026-08-19)** — the final isolated opt-in run completed `2 passed in 19.25s` against the configured `wan2.6-t2i` and `qwen-vl-max` models. It proved real remote image generation and download, full local image decoding/validation, managed `output_image` persistence, strict visual-assessment parsing, provider request-ID presence and numeric usage projection. The content remained in human `review`; the advisory result did not auto-approve it. No credential value was printed or persisted in project files.
- Seven-day real UAT: **not_run**.

## Phase A execution status

- Formal Phase A business specification and implementation: **complete (2026-08-20)** — single-account reports cannot create opportunities; two/three-account levels, immutable evidence ownership, pending review, one-way approval/rejection, and the approved-opportunity product gate are covered by backend/frontend/controlled E2E tests.
- Identity-preserving isolated Stage 2 baseline: **passed** — one real ranked account, one trusted public profile, 62 persisted public notes, and one trusted shop artifact at `2/2/2`, zero missing, `complete=true`; existing historical `needs_human` tasks remain unchanged.
- Real Qianfan import into the isolated Phase A database: **passed** — 8 scopes, 80 items and 71 ranked account projections through the trusted radar ingestion service.
- Second independent account: **retained but removed from real candidates** — one distinct clothing/physical-goods account has a trusted profile and 10 persisted latest-note sample facts. It is now classified `out_of_scope_physical`. Android later observed 18 deduplicated product links, but those links remained only in the discovery process: the job has 66 screenshots, 66 UI hierarchies, zero `shop_collection_result` artifacts and zero reusable persisted `source_url` fields. Current trusted product sample is `0/3`, not `3/3`; no phone rescan was performed to conceal this gap, and test override evidence cannot qualify for a real Opportunity.
- Real cross-account cluster and human review: **not_run / not fabricated** — the second account lacks trusted shop evidence. No real `pending_review`, `warming_candidate`, approval or rejection was claimed. See `docs/PHASE_A_UAT_REPORT.md`.
- [x] Dedicated visible Chrome + manual login + localhost CDP production control: `real-account-A` produced a succeeded durable account job, one trusted artifact and a latest-10 profile/note snapshot; a restarted Database/Service read the same ten owner-bound notes.
- [ ] Next ranked candidate shop gate: its CDP profile/latest-10 persisted successfully, but the real Android preflight stopped at 2/3 with `needs_human/selector_changed` and 26 Android artifacts. Do not count it as a qualified account or retry beyond the approved bounded run.
- [x] Persist account-level physical-scope decisions without rewriting historical jobs; restart-read and pre-Android skip verified against the REPor evidence.
- [x] Treat preflight as at most three representative products; deterministic physical evidence after item 1 stops before item 2/3, while ambiguous evidence remains fail-closed.
- [ ] Ranked continuation after the correction: 17 new accounts were inspected before stopping the overrun. Eight ended as first-item `out_of_scope_physical`; one digital preflight passed but full discovery stopped truthfully at 2 items with `selector_changed`. No new qualified account, N/N shop, cross-account analysis, opportunity, or pending review exists.

Until every item above is evidenced, status is “software implemented / awaiting
real UAT”, not “same effect as the tutorial proven”.

## Controlled account/note Task 5 evidence

- Frontend unit tests cover account/search empty, queued, terminal, needs-human, stale-read, single-flight, bounded polling, explicit same-job resume, lock release and unmount cancellation behavior.
- A fake authenticated CLI contract executes the fixed `python -I <repo-readonly-wrapper>` argv, sends prepared cookies only on stdin, runs `status` without `--json`, validates identity through real-shape `whoami --json`, and then reaches exact `user`, `user-posts` and `search` commands using pinned top-level-list, `userPageData/userInfo` and nested `noteCard` shapes. Missing or untrusted external state remains non-success and creates no database facts.
- The Playwright fixture starts with no account profile/note rows. Each run creates a unique ranked account, starts the real Task 3 account collection route from the UI, runs the production XHS adapter against pinned-CLI shapes, persists its profile/note through the reserved job/artifact/database path, confirms xsec material is absent from returned job facts, selects the resulting `account-note:*` evidence for analysis, then completes the existing shop N/N, opportunity, product, content review and available ZIP path.
- The controlled E2E passed once and with `--repeat-each=5`; this is software evidence only and does not satisfy the live gates above.

## Controlled Bailian media Task 5 evidence

- Content Studio submits only current revision, image-plan entry and managed material IDs. It shows durable queued/running/needs-human/failed/succeeded/cancelled facts, provider/model/duration/usage, sanitized failures, retry controls and available managed `output_image` identities.
- Visual output is labelled “AI visual advice — human review still required”. It never fills a human visual-check field and never invokes approval.
- The fresh-runtime Playwright fixture no longer writes the old `fixtures/cover.png` placeholder. A controlled image adapter returns real decodable PNG bytes through the production API, reserved worker, database, cleanup reservation and managed file/material path. The browser then analyzes that generated material, rejects the planning draft, creates the final draft from the generated material, performs explicit human checks, approves and exports an available ZIP.
- This controlled path remains separate software evidence. The guarded live gate independently proved real Bailian image generation and durable strict visual completion on 2026-08-19; it did not test the Bailian text model or replace the remaining platform/device/seven-day UAT gates.
