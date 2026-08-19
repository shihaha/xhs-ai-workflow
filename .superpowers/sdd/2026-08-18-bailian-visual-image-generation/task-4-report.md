# Task 4 report — reserved media worker, API and lifecycle

## Scope delivered

- Added strict write-only submission routes for image generation and advisory visual analysis. Request bodies accept only the expected revision, one image-plan entry, or bounded managed material IDs; unknown model, endpoint, URL and path fields are rejected.
- Added read-only media-run list/detail routes with truthful unavailable, missing, stale/conflict and validation responses.
- Reserved `content_image_generation` and `content_image_analysis` plus their provenance/assessment artifacts from generic Jobs creation, claim, transition, log and artifact mutation.
- Added one app-owned daemon worker with bounded queue polling, durable run leases, serialized execution, queued-run startup recovery and fail-closed operator recovery for expired running work.
- Added a final execution admission fence before provider-returned facts or bytes can persist. Shutdown cancels queued/running runs; a provider returning after bounded close cannot turn a cancelled run into success.
- Wired one shared `ContentMediaService`, worker and separately configured Bailian vision/image adapters into the application factory and lifespan.
- Added separate `bailian_text`, `bailian_vision` and `bailian_image` health facts while retaining the existing compatibility check.

No frontend, XHS, research, automatic approval, publication or request-path delete behavior was added. No live provider call or API key was used.

## TDD evidence

Initial RED:

```text
python -m pytest backend/tests/media/test_media_api.py backend/tests/media/test_media_worker.py backend/tests/test_health.py backend/tests/test_jobs_hardening.py -q
ERROR backend/tests/media/test_media_api.py
ModuleNotFoundError: No module named 'backend.app.features.media.api'
ERROR backend/tests/media/test_media_worker.py
ModuleNotFoundError: No module named 'backend.app.features.media.worker'
```

Focused GREEN after the minimal implementation:

```text
python -m pytest backend/tests/media/test_media_api.py backend/tests/media/test_media_worker.py backend/tests/test_health.py backend/tests/test_jobs_hardening.py -q
18 passed in 5.49s
```

The first full regression found one compatibility failure in an older XHS lifespan test whose synthetic app state omitted the new optional media-worker attribute. A one-line optional lookup fixed that test without changing production behavior.

## Final verification

```text
python -m pytest backend/tests/media backend/tests/test_jobs_api.py backend/tests/test_jobs_hardening.py backend/tests/test_health.py -q
95 passed, 104 warnings in 21.94s

python -m pytest backend/tests/xhs/test_s2a_round1_hardening.py::test_lifespan_does_not_ignore_unsafe_xhs_close backend/tests/media/test_media_api.py backend/tests/media/test_media_worker.py backend/tests/test_health.py backend/tests/test_jobs_hardening.py -q
20 passed in 6.01s

python -m pytest backend/tests -q
1361 passed, 2 skipped, 144 warnings in 265.31s
```

The skips are guarded live checks. Warnings are the existing Python 3.12 SQLite datetime-adapter deprecations; there were no final failures.

```text
python -m py_compile backend/app/features/media/api.py backend/app/features/media/worker.py backend/app/features/media/service.py backend/app/main.py backend/app/api/jobs.py backend/app/api/health.py
git diff --check
```

Both commands exited successfully before the report update. Live Bailian media validation remains `not_run`.

## Deferred by the approved scope boundary

- Content Studio controls, browser E2E and guarded live Bailian validation remain Task 5.
- There is no application authentication/authorization layer in the approved Task 4 scope, so no new artificial 403 mechanism was introduced. Existing local API trust boundaries are unchanged.

## Review fix round 1 — restart recovery ignores stale future leases

The bounded review found that startup recovery selected queued work and only expired running work. A run left by a dead process with a future lease could therefore remain `running` until that timestamp, or indefinitely after clock drift.

A RED test created a running run with a lease one hour in the future and proved it remained stranded. The worker now takes one startup-only snapshot of every pre-existing queued or running media run. Queued work is resumed once; every pre-existing running run is moved fail-closed to `needs_human` without replaying a paid provider call. Runs claimed after startup are not part of that snapshot and keep the normal single-worker lifecycle.

Fix verification:

```text
python -m pytest backend/tests/media backend/tests/test_jobs_api.py backend/tests/test_jobs_hardening.py backend/tests/test_health.py -q
96 passed, 104 warnings in 21.81s

python -m pytest backend/tests -q
1361 passed, 2 skipped, 144 warnings in 267.21s
```

Live Bailian remains `not_run`; no frontend, XHS or research file was touched.
