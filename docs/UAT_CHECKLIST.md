# Live UAT checklist

Automated and controlled-fixture checks cannot pass these gates. Record dates,
operator, job IDs, evidence paths, package ID/hash, and any recovery action.

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
- [ ] Run a two-account demand cluster and require every candidate to cite trusted shop and note evidence from both accounts.
- [ ] Confirm a valid two-account candidate is `warming_candidate + pending_review` and that the evidence count is server-derived.
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
- Ranked-account profile/note continuation: **blocked by real public-profile read (2026-08-20)** — the authenticated local session probe passed and `user-posts` returned 5 public notes for each of two ranked candidates, but the pinned CLI `user` command failed for both (`cli_failed`; one bounded 20-second attempt first timed out). No profile/note facts were persisted. Existing trust rules correctly forbid substituting request input or note-owner text for a verified public profile.
- Bailian text live contract: **failed (2026-08-19)** — the configured text model and Key reached Bailian successfully with HTTP 200, but the returned JSON did not satisfy strict `AnalysisOutput` and was rejected as `model_output_invalid` after 7.796 seconds. The production adapter now sends the exact Pydantic JSON Schema and JSON-only/key-preservation instruction; focused contract tests pass, but the real provider output remains non-conformant. No model output, Key or full provider payload was persisted in this record, and no successful analysis fact is claimed.
- Bailian image/vision live gate: **passed (2026-08-19)** — the final isolated opt-in run completed `2 passed in 19.25s` against the configured `wan2.6-t2i` and `qwen-vl-max` models. It proved real remote image generation and download, full local image decoding/validation, managed `output_image` persistence, strict visual-assessment parsing, provider request-ID presence and numeric usage projection. The content remained in human `review`; the advisory result did not auto-approve it. No credential value was printed or persisted in project files.
- Seven-day real UAT: **not_run**.

## Phase A execution status

- Formal Phase A business specification: **approved / implementation in progress (2026-08-20)**.
- Existing Stage 2 trusted account baseline: **passed** — one real ranked account, one public profile, 62 persisted public notes, and one new successful shop verification at `2/2/2`, zero missing, `complete=true`; the older `needs_human` shop job remains unchanged.
- Second independent account and real cross-account cluster: **not_run**.

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
