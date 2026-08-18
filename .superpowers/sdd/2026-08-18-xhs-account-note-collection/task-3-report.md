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

## Fix round 1/5 — secret shapes, transaction outcome and lifecycle fences

Commit: `HEAD` (`fix: harden xhs collection finalization`; one fix-round commit).

### RED

```text
python -m pytest backend/tests/xhs/test_cli_adapter.py backend/tests/xhs/test_collection_service.py -q -k "structured_header or search_rejects_conflicting or transient_read_failure or shutdown_fence_wins or conflicting_search_owner or historical_search_artifact"
FFFFFFF
7 failed, 62 deselected in 1.21s
```

The failures independently proved all four review findings:

- `headers: [{"name": "Cookie", "value": ...}]` survived both adapter and
  service redaction;
- a committed success whose acknowledgement and next two reads failed was
  followed by a failure payload overwrite of the successful artifact path;
- shutdown could set its fence while account facts were pending, yet the final
  CAS still committed `succeeded` and retained those facts;
- conflicting search `user_id`/`userId` aliases passed adapter normalization,
  service success finalization and historical artifact reads.

### GREEN

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py backend/tests/xhs/test_cli_adapter.py backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
106 passed in 8.77s
```

```text
python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
122 passed in 10.74s
```

```text
python -m pytest backend/tests -q
744 passed, 1 skipped, 32 warnings in 95.37s
```

`python -m compileall -q backend/app` and `git diff --check` exited 0.
The first two full-suite attempts each had only the existing shop asynchronous
POST timing assertion above its 0.2 second wall-clock threshold (0.219 and
0.203 seconds); that test passed alone, no shop code was changed, and the fresh
third full run above passed all non-live tests.

### Fixes and self-review

- Adapter and service now share one recursive redactor. It recognizes direct
  credential keys and semantic `name`/`value(s)` header entries while retaining
  ordinary entries such as `{"name": "title", "value": ...}`.
- Failure evidence uses a distinct `-failure.json` path and performs a durable
  running-state preflight before any write. If transaction outcome/readback is
  unknown it writes nothing; a later confirmed success is returned without
  touching its bound bytes.
- Final job CAS and commit now hold the same lifecycle lock that owns the
  shutdown admission fence. Once close has set the fence, pending artifact and
  account fact writes roll back and the close path can persist cancellation.
- Search normalization, service finalization and historical search reads all
  use `canonical_owner_id`; conflicting or malformed retained aliases fail
  closed instead of selecting the first field.

### Remaining live boundary

No authenticated live `xhs-cli` command was run. The existing live gate remains
Task 5 scope and is still truthfully `not_run` here.

## Fix round 2/5 — lifecycle lock order and exact credential names

Commit: `HEAD` (`fix: remove xhs collection lock inversion`; one fix-round
commit).

### RED

Barrier-based regression tests reproduced the review findings before the
implementation changed:

```text
python -m pytest backend/tests/xhs/test_cli_adapter.py::test_structured_header_name_value_credentials_are_redacted_without_false_positive backend/tests/xhs/test_collection_service.py -q -k "structured_header or lifecycle_lock_while_waiting or close_fences_a_submit or two_normal_submits"
3 failed, 2 passed
```

- a finalizer holding SQLite could not reacquire the lifecycle lock while a
  concurrent submit held that lock waiting for SQLite; it missed the one-second
  barrier and the submit later reached SQLite's lock timeout;
- close could not set its admission fence while a submit was paused inside the
  database create call;
- substring credential matching removed ordinary `session title` and
  `secret garden` values. The normal two-submit characterization already
  passed and remained part of the concurrency gate.

An additional strict RED assertion showed `access_token` still leaked after
the first exact-name implementation; adding its normalized exact name made that
test green without broadening matching back to substrings.

### GREEN

The four lifecycle interleavings pass together:

```text
python -m pytest backend/tests/xhs/test_collection_service.py -q -k "lifecycle_lock_while_waiting or close_fences_a_submit or shutdown_fence_wins or two_normal_submits"
4 passed, 16 deselected in 0.87s
```

Fresh Task 3 focused suite:

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py backend/tests/xhs/test_cli_adapter.py backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
109 passed in 9.14s
```

Fresh required XHS/jobs suite:

```text
python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
125 passed in 11.13s
```

Fresh full backend regression:

```text
python -m pytest backend/tests -q
747 passed, 1 skipped, 32 warnings in 90.69s
```

The skip remains the opt-in live gate; the warnings remain the existing Python
3.12 sqlite datetime adapter deprecations. `python -m compileall -q backend/app`
and `git diff --check` exited 0. This full run had no shop timing failure and no
shop test or implementation was changed.

### Fixes and self-review

- Submit now holds the lifecycle condition only while acquiring an admission
  generation/token and maintaining its active-admission count. Job creation,
  cancellation and executor submission happen without the lifecycle lock.
- Close first closes admission and increments the generation, then waits on a
  condition (which releases the lifecycle lock) for in-flight admissions to
  reconcile. A reservation created across the fence is cancelled and its
  caller receives `CollectionServiceClosed`; an accepted submit is registered
  before close snapshots/cancels futures.
- Final success keeps the existing SQLite-to-short-lifecycle-lock order. No
  lifecycle-lock-to-SQLite path remains, while the generation fence preserves
  the rule that close wins over late success and account facts roll back.
- Credential redaction now uses an exact, case-folded, underscore-normalized
  allowlist for header/credential names. `Cookie`, `Authorization`, `token`,
  `access_token` and the other enumerated credential names are removed, while
  ordinary semantic names containing words such as session or secret survive.

### Remaining live boundary

No authenticated live `xhs-cli` command was run. Task 5 still owns that
explicit opt-in gate, so the live boundary remains `not_run`.
