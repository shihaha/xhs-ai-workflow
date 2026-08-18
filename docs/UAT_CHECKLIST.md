# Live UAT checklist

Automated and controlled-fixture checks cannot pass these gates. Record dates,
operator, job IDs, evidence paths, package ID/hash, and any recovery action.

## Prerequisites

- [ ] Authenticated current Qianfan browser profile supplied and health checked.
- [ ] Xiaohongshu login supplied without storing credentials in Git.
- [ ] One Android phone connected; ADB/device/app state reports actual availability.
- [ ] Bailian API key supplied through environment only; configured model call succeeds.
- [ ] Fresh runtime/database selected; all business lists are initially empty.

## Real end-to-end gate

- [ ] Run all eight Qianfan scopes; require persisted 8/8 scope facts, raw evidence and no fabricated percentage.
- [ ] Select a real account and collect its store on the real phone.
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
- Android real-device collection: **not_run: device unavailable**.
- Bailian live contract: **not_run: BAILIAN_API_KEY unavailable**.
- Seven-day real UAT: **not_run**.

Until every item above is evidenced, status is “software implemented / awaiting
real UAT”, not “same effect as the tutorial proven”.
