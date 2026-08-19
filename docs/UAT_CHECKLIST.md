# Live UAT checklist

Automated and controlled-fixture checks cannot pass these gates. Record dates,
operator, job IDs, evidence paths, package ID/hash, and any recovery action.

## Prerequisites

- [ ] Authenticated current Qianfan browser profile supplied and health checked.
- [x] Xiaohongshu login supplied without storing credentials in Git.
- [x] Trusted local `xhs-cli` state is prepared outside the application under an isolated directory inside the configured runtime. Set `XHS_LIVE_STATE_DIR` to that directory; do not paste Cookie, token or password into HTTP, tests, logs or this checklist.
- [ ] One Android phone connected; ADB/device/app state reports actual availability.
- [x] Bailian API key supplied through environment only; the configured image and vision model calls succeeded in the bounded media gate.
- [x] Explicit `BAILIAN_MEDIA_LIVE_TEST=1`, `XHS_BAILIAN_IMAGE_MODEL` and `XHS_BAILIAN_VISION_MODEL` were supplied only to the one-off test process. The key was not pasted into chat, HTTP payloads or logs.
- [ ] Fresh runtime/database selected; all business lists are initially empty.

## Real end-to-end gate

- [ ] Run all eight Qianfan scopes; require persisted 8/8 scope facts, raw evidence and no fabricated percentage.
- [ ] Select a real account and collect its store on the real phone.
- [ ] From that ranked account's UI, run read-only account collection and verify one public profile plus exact note N/N, source links, reserved job, raw artifact hash and database rows agree.
- [ ] Run one public-note keyword search and verify expected N, returned N, source links and the hash-bound search artifact agree.
- [ ] If account/search polling reaches its UI bound, confirm the old non-terminal job remains visible, the new-job action is released, and “continue refreshing” reads only that job's returned ID.
- [ ] Confirm any `account-note:*` entry marked ineligible is labelled stale/untrusted, disabled for analysis selection and retained for human audit.
- [ ] Select the new account's canonical `account-note:*` IDs together with its trusted shop evidence for analysis; confirm notes enrich claims but do not replace the exact shop N/N opportunity gate.
- [ ] Confirm declared N, discovered N, verified N, missing list and image manifests agree.
- [ ] Generate a grounded analysis and opportunity; every claim cites persisted evidence.
- [ ] Create product/materials/content, review it, and export an available ZIP.
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

## Current execution status (2026-08-19)

- Qianfan authenticated live collection: **not_run** — no authenticated verified profile/selectors were supplied.
- Xiaohongshu account/note bounded live gate: **passed (2026-08-19)** — the isolated opt-in run completed `1 passed in 89.83s`. It verified the authenticated current-account profile, exactly 3 public notes, the reserved job/raw artifact/database path, and an exact empty keyword-search result (0/0), using only fixed `status`, `whoami`, `user`, `user-posts` and `search` reads. The real `user-posts` response used nested page slots and was normalized without treating empty slots as notes. Credentials remained outside argv, logs and project files. A non-empty live search and the ranked-account UI-to-analysis workflow remain pending and are not implied by this bounded pass.
- Android real-device collection: **not_run: device unavailable**.
- Bailian text live contract: **not_run** — this bounded validation exercised only image generation and visual assessment, not the text model.
- Bailian image/vision live gate: **passed (2026-08-19)** — the final isolated opt-in run completed `2 passed in 19.25s` against the configured `wan2.6-t2i` and `qwen-vl-max` models. It proved real remote image generation and download, full local image decoding/validation, managed `output_image` persistence, strict visual-assessment parsing, provider request-ID presence and numeric usage projection. The content remained in human `review`; the advisory result did not auto-approve it. No credential value was printed or persisted in project files.
- Seven-day real UAT: **not_run**.

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
