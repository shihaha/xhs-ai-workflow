# Task 2 report — durable media runs and migration

## Scope delivered

- Added `content_media_runs` with durable owner product, content item, revision, image-plan entry, reserved job, capability, provider/model/prompt version, input digest, allowed evidence/material identities, attempts, usage, duration, output identity, sanitized failure facts and timestamps.
- Added a partial unique index that permits only one queued/running generation for the same item, revision and image-plan entry while retaining terminal runs for audit and retry.
- Added physical outcome and lease checks so queued/running rows cannot claim outputs, succeeded generation/analysis rows require their respective material/artifact, and failed/needs-human/cancelled rows cannot claim success.
- Added a small persistence store with job-aligned versioned CAS transitions, canonical lease ownership, list/get operations and recovery discovery for queued or expired-running work.
- Generation completion verifies an `output_image` belongs to the run owner; analysis completion verifies its artifact belongs to the reserved job.
- Create and transition commit acknowledgement loss uses a fresh exact read-back and returns success only when the intended run and job facts are proven.
- Added migration marker `content_media_runs_v1`. Fresh and unrelated populated legacy databases install it idempotently. Marker-present startup is validation-only; missing indexes/checks, markerless half schemas and malformed persisted bindings fail closed without repair.

No provider calls, files, cleanup operations, worker, API, frontend, content workflow, XHS or research files were changed. No key, arbitrary endpoint, URL or path is accepted by the create/read contracts.

## TDD evidence

Initial RED:

```text
python -m pytest backend/tests/media/test_media_schema.py backend/tests/media/test_media_migration.py -q
ERROR backend/tests/media/test_media_schema.py
ModuleNotFoundError: No module named 'backend.app.features.media'
```

Later RED cycles separately demonstrated missing generation/analysis completion, lost commit-ack recovery, needs-human/cancel/list behavior and strict read validation before each minimal implementation.

Focused GREEN:

```text
python -m pytest backend/tests/media backend/tests/content/test_content_schema_migration.py backend/tests/content/test_review_audit_migration.py -q
42 passed, 88 warnings in 6.28s
```

The warnings are the existing Python 3.12 SQLite datetime-adapter deprecation.

## Final verification

```text
python -m pytest backend/tests -q
1338 passed, 2 skipped, 128 warnings in 253.51s (0:04:13)
```

The two skips are guarded live checks. There were no failures.

```text
python -m py_compile backend/app/features/media/models.py backend/app/features/media/schemas.py backend/app/features/media/store.py backend/app/db.py
git diff --check
```

Both commands exited successfully. Live Bailian remains `not_run`; Task 2 is persistence-only.

## Deferred by the approved scope boundary

- Provider execution, generated-file/material transaction work and visual trust checks belong to Task 3.
- Reserved worker lifecycle, HTTP routes and health wiring belong to Task 4.
- Content Studio and real Bailian validation belong to Task 5.

## Review fix round 1 — caller failure text sanitization

The independent review found that `fail` and `needs_human` accepted caller-provided category/detail strings and persisted them verbatim. A RED test demonstrated that a credential sentinel, URL and Windows path all reached `content_media_runs`.

The persistence boundary now maps only known internal categories to stable safe categories (`unconfigured`, `authentication_failed`, `provider_unavailable`, `provider_rejected`, `invalid_output`, `provider_failure`, `validation_failed`, `trust_changed`, `transaction_unknown`, `state_changed`, `safety_rejected`). Unknown input becomes `internal_failure`. Caller detail is discarded; the database receives only `Media run failed.` or `Media run requires operator review.` according to the terminal state.

Fix verification:

```text
python -m pytest backend/tests/media backend/tests/test_jobs_hardening.py backend/tests/content/test_content_schema_migration.py -q
45 passed, 104 warnings in 6.77s

python -m py_compile backend/app/features/media/store.py backend/tests/media/test_media_schema.py
git diff --check
```

Both commands exited successfully. No migration, worker, API, frontend, XHS or research change was made.
