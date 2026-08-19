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

No production XHS implementation or `research/` file was changed. Task 5's
initial implementation did not make a live call; the separate guarded gate was
later run with explicit local authority and passed on 2026-08-19.

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

The final repeat-five run on 2026-08-20 initially exposed repository `.env`
leakage into the E2E fixture's temporary XHS state directory. The fixture now
explicitly places that state inside its owned temporary runtime; the rerun
passed 5/5. The guarded media gate without explicit process authority remains
exactly `not_run` (`1 passed, 1 skipped`) and creates no live success.

## Final verification

```text
scripts/verify.ps1
backend: 1378 passed, 3 skipped, 144 warnings in 289.06s
frontend: 8 files / 51 tests passed
production build: passed
controlled fresh-runtime E2E: 1 passed
dependency audit: found 0 vulnerabilities
tracked-file secret scan: clean
boundary source scan: clean

python -m pytest backend/tests/integration/test_bailian_media_live.py -q
1 passed, 1 skipped in 0.35s (default, no explicit live authority)

git diff --check
clean
```

The official full gate was run with the repository `.env` reversibly hidden so
temporary-runtime tests could not inherit machine-specific paths. The file was
restored in `finally`; the backup path is absent. No secret value was read,
printed or modified.

## Live status

- Bailian image generation: **passed (2026-08-19)** with real `wan2.6-t2i`
  generation, download, decoding and managed persistence.
- Bailian visual assessment: **passed (2026-08-19)** with real `qwen-vl-max`
  strict advisory output, request ID and numeric usage; content remained in
  human review. The isolated gate completed `2 passed in 19.25s`.
- Bailian text: attempted separately and failed safely as
  `model_output_invalid`; it is outside Task 5's media success claim.
- Authenticated Qianfan and the bounded current-account/three-note XHS gate have
  passed separately. A non-empty XHS search/ranked-account workflow, Android
  device and seven-day UAT remain `not_run` and are not implied by Task 5.
