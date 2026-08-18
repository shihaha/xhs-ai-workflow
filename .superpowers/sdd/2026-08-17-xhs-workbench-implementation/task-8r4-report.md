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
- The initial worker used a daemon thread and bounded join. That historical
  shutdown implementation is superseded by fix round 1 below; the bounded batch,
  interruptible poll and sanitized fault behavior remain current.
- After successful database construction, the app creates one
  `ArtifactCleanupService` with the configured grace period, recovers expired
  leases, injects that exact instance into `ContentService`, and constructs the
  worker. The original shutdown order is superseded by fix round 1 below.
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

## Fix round 2/5: final rename authorization boundary

Independent review found one remaining time-of-check/time-of-use gap: cleanup
checked cancellation immediately before calling the rename helper, but the helper
then performed several blocking handle/identity operations before the actual
Windows atomic rename.

RED evidence:

- A real helper-internal pre-rename block ignored worker shutdown and allowed the
  later Windows move.
- A shutdown immediately after the atomic OS move had no explicit contract to
  persist the already-moved path and identity before SQLite finalization.

Implementation:

- `rename_contained_regular_to_directory` now accepts an optional authorization
  callback while preserving all direct callers by default. After source and
  target-directory handles and their identities are prepared, it invokes the
  callback at the nearest possible point before `_rename_open_file`. Rejection
  raises the dedicated `ArtifactRenameCancelled` and closes both handles without
  invoking the OS rename. Non-Windows behavior remains fail closed.
- Narrow pre-atomic and post-atomic hooks exist only for deterministic boundary
  fault injection. Tests use the real helper and real Windows handle path rather
  than replacing the rename function.
- Cleanup passes the worker fence into the helper. Cancellation before the OS
  mutation leaves the original bytes and claimed lease untouched. If cancellation
  arrives after the atomic move completed, cleanup performs the one necessary
  safety acknowledgement: it persists the exact quarantine path/physical identity
  as `needs_human/shutdown_after_quarantine_move` before the worker may close the
  database.

Focused verification:

```text
python -m pytest backend/tests/content/test_cleanup_worker.py backend/tests/content/test_artifact_cleanup_service.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q
89 passed in 12.70s

python -m pytest backend/tests/content -q
253 passed in 34.97s

python -m pytest backend/tests -q
566 passed, 1 skipped in 53.42s
```

The focused suite also verifies the uncancelled callback path still performs the
normal quarantine rename and cancellation closes handles. Live Bailian, Android
and seven-day UAT remain `not_run`; Task 8R-5 has not started.
