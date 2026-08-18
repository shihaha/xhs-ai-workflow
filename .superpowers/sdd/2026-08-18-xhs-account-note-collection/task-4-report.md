# Task 4 report — analysis trust and account-note grounding

## Scope delivered

- Added canonical `account-note:<sqlite-id>` evidence IDs to strict analysis input
  and model-output citation validation.
- Analysis discovery now projects persisted account notes instead of exposing the
  underlying XHS account raw artifact as a selectable dead-end ID.
- Every selected note is re-verified through its persisted account snapshot,
  succeeded reserved job, fixed artifact kind/producer/path, bounded contained
  regular file, stable physical identity, byte size/SHA-256, artifact metadata,
  normalized `CollectionResult`, account ownership, normalized row values and
  canonical raw-evidence digests.
- The model receives only canonical evidence IDs and normalized public profile/note
  fields. Raw payloads, artifact metadata, job IDs, local paths and other accounts
  are not included.
- Account notes can enrich grounded claims. They do not change the existing exact
  complete shop N/N gate for `product_cluster` or `account_opportunity` requests.

## RED evidence

Initial Task 4 tests, before production changes:

```text
python -m pytest backend/tests/analysis/test_account_note_grounding.py -q --tb=short
17 failed, 4 passed in 2.71s
```

The failures showed that valid account-note IDs were rejected by the canonical
schema and no account-note discovery rows existed. Cross-account, tamper,
producer/kind/job, path/hash/size/metadata, raw-digest, public-payload and shop-gate
tests therefore could not reach the missing feature.

A later discovery mutation test independently exposed the unusable raw-artifact
dead end before its fix:

```text
python -m pytest backend/tests/analysis/test_account_note_grounding.py::test_discovery_returns_only_account_notes_owned_by_requested_account -q --tb=short
1 failed in 0.50s
```

The unfiltered endpoint returned `artifact:*` IDs for XHS account raw snapshots
even though those IDs were trust anchors rather than selectable account facts.

## GREEN evidence

Focused Task 4 suite:

```text
python -m pytest backend/tests/analysis/test_account_note_grounding.py -q --tb=short
21 passed in 2.73s
```

Task 4 plus the pre-existing grounding and structured-output contracts:

```text
python -m pytest backend/tests/analysis/test_account_note_grounding.py backend/tests/analysis/test_evidence_grounding.py backend/tests/analysis/test_structured_output.py -q
54 passed in 5.41s
```

Fresh analysis and XHS regression:

```text
python -m pytest backend/tests/analysis backend/tests/xhs -q
364 passed, 1 skipped in 14.06s
```

Fresh full backend regression:

```text
python -m pytest backend/tests -q
956 passed, 1 skipped, 32 warnings in 94.92s
```

The skip is the existing explicit opt-in live gate. The warnings are the existing
Python 3.12 SQLite datetime-adapter deprecations in content tests.

`python -m compileall -q backend/app backend/tests` and `git diff --check` exited 0.

## Self-review

- Unknown, malformed, duplicate, stale, cross-account and untrusted account-note
  IDs fail the whole request before the model. Cross-account ownership keeps its
  distinct `EvidenceAccountMismatch` contract.
- A selected note is trusted only when the whole current account snapshot still
  matches its exact profile-plus-N-note artifact and every persisted note in that
  snapshot. A valid row cannot hide a missing or tampered sibling.
- Discovery recomputes trust at read time. Its eligibility flag means the note is
  a trusted selectable input; opportunity creation still independently requires
  complete shop coverage for every requested account.
- Existing artifact/rank evidence, strict whole-response grounding, provider-neutral
  model failure handling, analysis persistence and migration contracts were left
  unchanged.
- No Task 5 UI/E2E work, job lifecycle changes, Bailian media work, platform writes
  or untracked research files are included.

## Remaining live boundary

No authenticated live `xhs-cli` command was run. The explicit Task 5 live gate
remains truthfully `not_run`; this task is verified with controlled persisted
account collections and backend tests only.

## Fix round 1/5 — permanent identities and fail-closed discovery

### Review findings closed

- `xhs_account_notes.id` is now a physical SQLite `AUTOINCREMENT` identity. A
  separate `xhs_account_note_identity_v2` migration validates the actual table
  DDL and `sqlite_sequence`, preserves every existing row ID during a v1 rebuild,
  and advances the sequence beyond both current rows and canonical historical
  `account-note:*` citations persisted in analyses or opportunities.
- The identity migration is retry-safe and fail-closed. With the marker present it
  is validation-only; with the marker absent it first validates the complete v1
  evidence schema and data, then runs the rebuild and marker write under one
  migration transaction. A present marker paired with old DDL or a stale or
  ambiguous sequence is rejected without repair; marker tampering and an
  interrupted temporary table also fail closed.
  Malformed persisted reference history is also rejected during the read-only
  preflight so it cannot hide a previously cited note ID.
- Recollection can no longer bind an old evidence ID to a new note. Tests cover a
  stale historical citation and an analysis that is concurrently inside the model
  call while recollection replaces the account snapshot.
- Unfiltered discovery now hides both `xhs_account_collection_raw` and
  `xhs_note_search_raw`. These remain trust anchors and are never exposed as
  unusable selectable `artifact:*` evidence; an empty successful search does not
  synthesize note evidence.
- Unknown or retired job-state strings are parsed safely. Filtered and unfiltered
  discovery mark the poisoned note ineligible, while selection fails before the
  model instead of raising an uncaught enum `ValueError`.

### Fix-round RED evidence

Before the fixes, the new migration regression group produced:

```text
5 failed, 4 passed
```

The failures demonstrated missing `AUTOINCREMENT`, identity marker and sequence
semantics, and unsafe legacy migration. A separate mutation run with the
historical-reference floor disabled produced `1 failed` because the next ID was
`1` even though a persisted historical citation referenced `account-note:41`.
A further fail-closed regression initially produced `1 failed` because malformed
historical `evidence_ids_json` was skipped and the migration wrote new DDL instead
of rejecting the unreadable identity history before writes.

Before the discovery/state/identity production changes, the selected analysis
regressions produced:

```text
4 failed, 21 deselected
```

They reproduced search-raw discovery exposure, the uncaught unknown-state error,
old-ID rebinding after recollection and concurrent reuse of the same row ID.

### Fix-round GREEN evidence

```text
python -m pytest backend/tests/xhs/test_schema_migration.py -q
11 passed

python -m pytest backend/tests/analysis/test_account_note_grounding.py -q
25 passed in 3.53s

python -m pytest backend/tests/analysis backend/tests/xhs -q
373 passed, 1 skipped in 17.00s

python -m pytest backend/tests -q
966 passed, 1 skipped, 32 warnings in 105.74s
```

The skip remains the explicit opt-in live gate. The 32 warnings remain the
pre-existing Python 3.12 SQLite datetime-adapter deprecations in content tests.
`python -m compileall -q backend/app backend/tests` and `git diff --check` exited
0 after the fix-round changes.

### Fix-round self-review and boundary

- The identity floor deliberately recognizes only canonical positive decimal
  `account-note:<id>` values. Malformed stored evidence values do not gain trust.
- Historical citations outside this database cannot be discovered by a local
  migration. Within the workbench database, current rows plus persisted analysis
  and opportunity citations are protected, and all future deletes retain the
  SQLite high-water mark.
- No model-provider behavior, exact shop N/N opportunity gate, Task 5 UI/E2E,
  Bailian media code, platform writes or untracked research files changed.
- The authenticated live boundary is unchanged: `not_run`.
