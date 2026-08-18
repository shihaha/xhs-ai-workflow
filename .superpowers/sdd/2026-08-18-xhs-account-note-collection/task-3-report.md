# Task 3 report — collection service, reserved jobs and API

## Scope delivered

- Commit: `HEAD` (`feat: run durable account note collections`; one Task 3 commit).
- Added a daemon-serial XHS collection service with an admission/close fence,
  restart recovery, bounded shutdown join, scheduling-failure finalization and
  cancellation checkpoints before the external call and before DB finalization.
- Account requests pass `expected_note_count + 1` to `fetch_account`. Exact
  success adds and flushes the reserved raw artifact, calls
  `persist_exact_account_result`, performs the `running -> succeeded` CAS and
  commits all database facts in the same session.
- Search jobs use their own job/artifact types and store a complete normalized
  result in the hash-bound raw artifact. Search reads revalidate path, size,
  SHA-256, job binding, producer, N/N and note-only shape; they never write or
  expose account-owned profile/note rows.
- Added strict HTTP 202 submit routes and public-only profile, account-note and
  search-result reads. Raw evidence and credentials are excluded from read APIs.
- Registered the settings-owned `XhsCliReadAdapter`, wired service lifecycle,
  and reserved both XHS job types/artifact kinds from generic Jobs API mutation.

## RED evidence

Initial focused command:

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py -q
```

Observed expected collection failure:

```text
ModuleNotFoundError: No module named 'backend.app.features.xhs.service'
1 error in 0.57s
```

After the service tests became green, the API/lifecycle tests independently
failed because `app.state.xhs_collection_service` did not exist (`2 failed`),
and the registry test failed because `build_default_registry` did not exist.

Additional security RED probes found real defects before their fixes:

- leaky successful adapter results persisted `secret-sentinel` in both raw
  artifact and normalized raw evidence;
- a malformed adapter return raised `AttributeError` and left the job running;
- a tampered search artifact was returned as a normalized read fact;
- account artifact metadata did not separate note N/N from profile-plus-note
  item N/N.

## GREEN evidence

Focused Task 3 and adjacent jobs/registry tests after implementation:

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py backend/tests/test_adapter_registry.py backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
52 passed in 8.03s
```

Fresh required XHS/jobs focused suite:

```text
python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
115 passed in 9.63s
```

Fresh full backend regression:

```text
python -m pytest backend/tests -q
737 passed, 1 skipped, 32 warnings in 92.26s
```

The one skip is the existing opt-in live gate. The 32 warnings are the existing
Python 3.12 sqlite datetime adapter deprecations in content tests.

## Self-review

- Generic jobs can read and purely cancel the two XHS types, but cannot create,
  claim, append logs, attach artifacts, inject cancellation fields or force a
  successful transition.
- Cancellation that wins before finalization leaves no account facts and no
  artifact row. The already-written uniquely named evidence file is retained as
  an unreferenced recovery artifact because SQLite and NTFS cannot commit
  atomically; it is never returned as durable evidence.
- A lost success commit acknowledgement is resolved by a read-only durable
  state/artifact digest check, so a committed success is not downgraded.
- Adapter outputs receive a second recursive credential-key redaction before
  either artifact or normalized fact persistence. Exception messages are never
  stored; only bounded category and exception class facts are retained.
- The service does not resume queued/running CLI work after restart; it moves it
  to `needs_human/worker_restart_required` for explicit operator action.

## Concerns

- No authenticated live `xhs-cli` invocation was run. Task 5 owns the explicit
  opt-in live gate; current evidence is controlled adapter/subprocess fixtures.
- Search normalized facts are intentionally stored in and read from the single
  immutable, hash-verified artifact because Task 2 introduced relational tables
  only for account profile/note ownership. A future search-history query/index
  requirement would justify a separate migration, but it is outside Task 3.
- External command cancellation is cooperative at the service boundary: a
  running subprocess is bounded by the trusted adapter timeout and its late
  return cannot write success; the service does not attempt unsafe thread or
  process termination.
