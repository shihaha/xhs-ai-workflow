# Task 3 report — generated-image persistence and visual trust

## Scope delivered

- Added `ContentMediaService` submission, execution and read boundaries for generation and advisory analysis, without adding worker, API or frontend behavior.
- Generation accepts only a current revision and one existing image-plan entry. The provider receives a server-built prompt and no endpoint, model, path or filename input.
- One validated provider image is written only to `content-generated/{item_id}/{run_id}/{material_id}.{ext}` with server-generated identities.
- A durable material cleanup reservation is committed before writing. The `output_image` material, sealed generation provenance (run/material/hash/MIME/size/dimensions/provider/model), cleanup cancellation, media-run success and reserved-job success commit together.
- Lost generation commit acknowledgement returns success only after a fresh exact proof. Non-landed or unknown outcomes retain the file and make the cleanup fact due; the request path never directly deletes it.
- Visual analysis accepts only current item images or generated outputs already bound to the same item/revision. Each file is bounded-read and checked before/after by managed owner, path, hash, size, MIME and physical identity.
- The durable visual-assessment artifact is sealed to the run, revision, model, exact material facts and provider request ID. Reads revalidate current content trust, file facts and the assessment seal and fail closed on drift.
- Visual output remains advisory. It does not create reviews, mark per-image checks passed or mutate the content item's status.

No worker, HTTP route, app lifecycle, health, frontend, XHS or research files were changed. No live provider call or API key was used.

## TDD evidence

Initial RED:

```text
python -m pytest backend/tests/media/test_generation_service.py backend/tests/media/test_visual_service.py -q
ERROR backend/tests/media/test_generation_service.py
ModuleNotFoundError: No module named 'backend.app.features.media.service'
ERROR backend/tests/media/test_visual_service.py
ModuleNotFoundError: No module named 'backend.app.features.media.service'
```

Subsequent RED cycles separately proved these missing boundaries before their minimal fixes:

- assessment metadata tamper remained readable;
- filesystem write failure left the run `running`;
- generated width/provenance tamper remained readable;
- visual commit-ack loss raised despite a committed success;
- content/product trust drift left old advice readable.

Focused GREEN:

```text
python -m pytest backend/tests/media/test_generation_service.py backend/tests/media/test_visual_service.py -q
............                                                             [100%]
12 passed in 4.14s
```

## Verification

```text
python -m pytest backend/tests/media -q
49 passed, 104 warnings in 8.62s

python -m pytest backend/tests/media backend/tests/content -q
345 passed, 136 warnings in 88.20s

python -m pytest backend/tests -q
1351 passed, 2 skipped, 144 warnings in 260.71s
```

The two skips are guarded live checks. Warnings are the existing Python 3.12 SQLite datetime-adapter deprecations; there were no failures.

```text
python -m py_compile backend/app/features/media/service.py backend/app/features/media/schemas.py backend/tests/media/test_generation_service.py backend/tests/media/test_visual_service.py
git diff --check
```

Both commands exited successfully. `ruff` was not available in the existing environment and was not installed. Live Bailian vision/image validation remains `not_run`.

## Review fix round 1 — returned vision model identity

The independent review found that a vision adapter could declare the reserved model as `declared-model-v1` but return a valid `VisionResult` naming `actual-provider-model-v2`; the service persisted the assessment while attributing it to the reserved model.

A single RED test reproduced that exact mismatch. The service now compares `VisionResult.model` with `ContentMediaRunRead.model` immediately after the provider returns and before post-call trust checks or persistence. A mismatch raises the fixed validation boundary, leaves no visual-assessment artifact, stores only the existing sanitized `validation_failed` run/job fact, and leaves the content status unchanged.

Fix verification:

```text
python -m pytest backend/tests/media/test_visual_service.py -q
7 passed in 2.66s

python -m pytest backend/tests/media backend/tests/content -q
346 passed, 136 warnings in 90.67s

python -m pytest backend/tests -q
1352 passed, 2 skipped, 144 warnings in 267.89s
```

Live Bailian remains `not_run`; no worker, API, frontend, XHS or research file was touched.

## Deferred by the approved scope boundary

- Reserved worker execution, HTTP routes, health and application lifecycle belong to Task 4.
- Content Studio, browser E2E and guarded live Bailian validation belong to Task 5.
