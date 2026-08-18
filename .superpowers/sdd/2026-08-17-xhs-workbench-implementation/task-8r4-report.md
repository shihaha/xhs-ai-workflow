# Task 8R-4 report: worker lifecycle and read-only cleanup API

## Scope

This task only wires the already approved durable cleanup service into the app,
runs one bounded background batch per poll, and exposes persisted cleanup facts
through read-only HTTP routes. It does not implement Task 8R-5 ZIP/model-order
minors, Task 9 UI, automatic publishing, or any force-delete API.

## RED evidence

- The new API/worker suite initially failed during collection because
  `ArtifactCleanupWorker` did not exist. At that point the application also had
  no cleanup service/worker state and no cleanup read routes.
- The tests define the missing contracts for strict list/detail reads, truthful
  empty/503/404 results, absence of POST/PATCH/DELETE routes, exact settings
  bounds, bounded batches, interruptible polling, bounded blocked shutdown,
  sanitized fault continuation, real due-record processing, expired-lease
  restart recovery, shared service injection and configured grace periods.

## Implementation

- Added the exact configured defaults and validation bounds: 30-second poll,
  batch size 10 and 24-hour grace, bounded to 1..3600, 1..100 and 1..168.
- Added one daemon `ArtifactCleanupWorker` owned by the FastAPI lifespan. It
  runs at most one `run_due_once(limit=batch_size)` call per poll, uses an Event
  so normal poll waits stop immediately, catches batch faults without retaining
  exception text, and continues at the next poll. A blocked external/file call
  cannot hold application shutdown indefinitely; all cleanup progress remains
  in the existing durable database lease/state machine.
- After successful database construction, the app creates one
  `ArtifactCleanupService` with the configured grace period, recovers expired
  leases, injects that exact instance into `ContentService`, and constructs the
  worker. Lifespan starts the worker and closes it before shop workers and the
  database.
- Added strict read-only `GET /api/v1/artifact-cleanups` and
  `GET /api/v1/artifact-cleanups/{id}` routes. Missing database state returns
  503, unknown records return 404, and no cleanup mutation route exists.
- Startup still performs no file deletion. The worker only claims due
  `pending`/`quarantined` records through the existing CAS service; future,
  claimed-by-another-worker and terminal states remain ineligible.

## Verification

```text
python -m pytest backend/tests/content/test_cleanup_api.py backend/tests/content/test_cleanup_worker.py backend/tests/test_health.py -q
14 passed in 1.74s

python -m pytest backend/tests/content -q
246 passed in 33.02s

python -m pytest backend/tests -q
559 passed, 1 skipped in 55.57s

python -m compileall -q backend/app backend/tests
git diff --check
```

The warnings are the repository's existing Python 3.12 SQLite datetime-adapter
deprecations. Live Bailian remains `not_run: BAILIAN_API_KEY unavailable`, live
Android remains `not_run: device unavailable`, and seven-day UAT remains
`not_run`. Compile and diff checks exited 0; Git emitted only the repository's
Windows LF/CRLF notices. Independent review is still required before acceptance.

## Fix round 1/5: shutdown fence and database lifetime

Independent review found that the original bounded join returned while a daemon
worker could still resume filesystem/database mutation, after which lifespan
immediately disposed the shared SQLite engine.

RED evidence:

- Five lifecycle regressions failed: no exact stopped finalizer/boolean close,
  stop-before-claim still claimed, stop-after-claim still moved bytes,
  pre-delete stop still deleted, and app lifespan closed SQLite before a blocked
  worker exited.

Implementation:

- The worker now owns a stop Event plus generation fence, closes admission before
  its bounded join, passes a fail-closed cancellation callback into every batch,
  returns whether it stopped, and exposes bounded `wait_stopped`. It is non-daemon
  and executes one sanitized `on_stopped` finalizer exactly once.
- Cleanup service direct calls remain backward compatible through a default
  never-cancelled callback. Worker calls checkpoint before claim, after blocking
  identity/reference/handle calls, around target and move/delete boundaries, and
  before/after database state CAS. Cancellation leaves an already claimed lease
  durable for expiry/restart recovery; it does not invent a terminal state.
- If shutdown arrives after an identity-bound delete was already armed, the
  existing safety rollback/disarm path still completes; otherwise no new file or
  database mutation begins after the fence closes.
- Lifespan closes shop admission first, then cleanup admission. The cleanup
  worker owns `Database.close` as its exact-once finalizer. If bounded close
  returns while a batch remains blocked, lifespan does not dispose SQLite; the
  worker closes it only after the cancelled batch has returned and can no longer
  mutate.

Verification:

```text
python -m pytest backend/tests/content/test_cleanup_worker.py backend/tests/content/test_cleanup_api.py backend/tests/content/test_artifact_cleanup_service.py backend/tests/test_health.py backend/tests/test_jobs_hardening.py -q
68 passed in 8.77s

python -m pytest backend/tests/content -q
251 passed in 34.74s

python -m pytest backend/tests -q
564 passed, 1 skipped in 52.89s
```

Live Bailian, Android and seven-day UAT remain `not_run`. This fix remains
limited to Task 8R-4; Task 8R-5 has not started.
