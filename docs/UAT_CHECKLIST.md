# Live UAT checklist

Automated and controlled-fixture checks cannot pass these gates. Record dates,
operator, job IDs, evidence paths, package ID/hash, and any recovery action.

## Prerequisites

- [ ] Authenticated current Qianfan browser profile supplied and health checked.
- [ ] Xiaohongshu login supplied without storing credentials in Git.
- [ ] Trusted local `xhs-cli` session is authenticated outside the application; do not paste Cookie, token or password into HTTP, tests, logs or this checklist.
- [ ] One Android phone connected; ADB/device/app state reports actual availability.
- [ ] Bailian API key supplied through environment only; configured model call succeeds.
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

## Current execution status (2026-08-18)

- Qianfan authenticated live collection: **not_run** — no authenticated verified profile/selectors were supplied.
- Xiaohongshu account/note live collection: **not_run** — `XHS_LIVE_TEST=1` and a trusted authenticated local `xhs-cli` session/targets were not supplied. The opt-in gate accepts only user ID, keyword and expected counts; it performs fixed read-only commands and never changes login state.
- Android real-device collection: **not_run: device unavailable**.
- Bailian live contract: **not_run: BAILIAN_API_KEY unavailable**.
- Seven-day real UAT: **not_run**.

Until every item above is evidenced, status is “software implemented / awaiting
real UAT”, not “same effect as the tutorial proven”.

## Controlled Task 5 evidence

- Frontend unit tests cover account/search empty, queued, terminal, needs-human, stale-read, single-flight, bounded polling, explicit same-job resume, lock release and unmount cancellation behavior.
- A fake authenticated CLI contract executes `status` without `--json`, validates identity through `whoami --json`, and then reaches the exact `user`, `user-posts` and `search` read path. A fake unauthenticated session remains `not_run` and creates no database.
- The Playwright fixture starts with no account profile/note rows. Each run creates a unique ranked account, starts the real Task 3 account collection route from the UI, persists its profile/note through the reserved job/artifact/database path, selects the resulting `account-note:*` evidence for analysis, then completes the existing shop N/N, opportunity, product, content review and available ZIP path.
- The controlled E2E passed once and with `--repeat-each=5`; this is software evidence only and does not satisfy the live gates above.
