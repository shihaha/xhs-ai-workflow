# Task 5 report — Content Studio, generated-image E2E and guarded live gate

## Scope delivered

- Added strict frontend media-run clients for generation, advisory analysis, item run history, run detail and sealed assessment reads.
- Added Content Studio controls for each current image-plan entry, managed generated-image selection, advisory visual analysis, truthful six-state run history, provider/model/duration/usage, sanitized failures and explicit retry.
- Generated outputs are shown only when their persisted product material is an available managed `output_image`; their material IDs can be used by the existing content-draft flow.
- Visual output is labelled as AI advice requiring human review. It never fills a human visual-check input and has no approval mutation.
- Preserved the existing manual material path but labelled it explicitly as a manual existing-file import.
- Added the one minimal Task 4 API gap: a read-only sealed visual-assessment route. It delegates to the existing trust-verifying service and has no approval side effect.
- Replaced the old controlled `fixtures/cover.png` placeholder. The E2E image adapter returns real decodable PNG bytes through the production API, reserved worker, database, cleanup reservation, managed file/material and UI path. The browser analyzes that output, explicitly rejects the planning draft, creates the final draft from the generated material, performs human checks, approves and exports an available ZIP.
- Added an explicit opt-in live media gate. It requires `BAILIAN_MEDIA_LIVE_TEST=1`, a local key and explicit image/vision model variables. Default outcome is exactly `not_run` and creates no successful run or file. It never publishes or logs credentials.
- Added responsive proof at 320, 768, 1024 and 1440 pixels. The RED check found long package hashes overflowing at 320 pixels; the minimal wrap fix passed all four widths.

No XHS implementation or `research/` file was changed. No live Bailian call was made.

## TDD evidence

Observed RED boundaries included:

- missing assessment HTTP route returned 404;
- missing frontend media functions raised `startContentImageGeneration is not a function`;
- missing media controls could not find the generation/retry/advice UI;
- the old E2E fixture never reached a succeeded generation run;
- repeated E2E initially exposed an unscoped multi-item selector;
- the responsive check exposed real 320-pixel horizontal overflow.

Focused GREEN:

```text
python -m pytest backend/tests/media backend/tests/integration/test_bailian_media_live.py backend/tests/test_jobs_hardening.py backend/tests/test_health.py -q
70 passed, 1 skipped, 104 warnings

npm test --prefix frontend -- --run
8 files passed, 51 tests passed

npm run build --prefix frontend
production build passed

npm run test:e2e --prefix frontend -- --repeat-each=5
5 passed
```

The one skip is the explicit guarded Bailian media live test and reports `not_run` without local authority.

## Final verification

```text
python -m pytest backend/tests -q
1362 passed, 3 skipped, 144 warnings in 255.28s

python -m pytest backend/tests/test_release_scanner.py backend/tests/test_release_hardening.py -q
8 passed

python tools/scan_release_boundaries.py --root .
clean

npm audit --prefix frontend --audit-level=high
found 0 vulnerabilities

git diff --check
clean (line-ending notices only)
```

An earlier full-backend attempt had one transient Windows process-cleanup result in the unrelated XHS timeout test (`process_cleanup_failed` instead of `timeout`). Its immediate focused rerun passed, no XHS code was changed, and the complete backend rerun above passed.

## Live status

- Bailian image generation: `not_run`.
- Bailian visual assessment: `not_run`.
- Authenticated Qianfan, real XHS session, Android device and seven-day UAT remain `not_run`.
