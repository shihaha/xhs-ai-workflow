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

## Fix round 2/5 — structural DDL proof and bounded identity preflight

### Review findings closed

- Identity validation no longer searches normalized source text for an
  `AUTOINCREMENT` substring. A SQLite-aware tokenizer removes line and block
  comments, isolates string literals, preserves quoted identifiers as identifiers,
  and splits the real table body into top-level column declarations. Validation
  now requires the actual `id` column to declare `INTEGER`, `PRIMARY KEY` and
  `AUTOINCREMENT`; comments, literals and unrelated quoted identifiers cannot
  authorize the marker. A genuinely quoted `"id"` column remains valid.
- A present `xhs_account_note_identity_v2` marker remains validation-only. A
  writable-schema test replaces the physical ID declaration with non-AUTOINCREMENT
  DDL plus a spoofing comment and verifies initialization fails without changing
  DDL, marker, rows or sequence state.
- Historical `account-note:*` suffixes are parsed only after a canonical decimal
  and explicit SQLite rowid bound check. Zero, signs, leading zeroes, non-digits,
  values above `9223372036854775807` and overlong digit strings all raise a uniform
  `SchemaMigrationError` during the read-only preflight; no Python integer or
  SQLite binding exception can occur after a rebuild.
- The read-only preflight now covers the complete evidence schema/data, all
  persisted analysis and opportunity citation lists, unfinished migration-table
  presence, and repairability of an existing AUTOINCREMENT sequence. It is rerun
  before the first migration write. A database with new DDL plus the old temporary
  table fails identically on repeated restarts and leaves the asserted DDL, marker,
  row and sequence state exactly unchanged.

### Fix-round RED evidence

Before production changes, the selected new regressions produced:

```text
12 failed, 2 passed, 11 deselected
```

The failures showed block and line comments authorizing a non-AUTOINCREMENT ID,
a real quoted `"id"` declaration being rejected, marker-present spoofed DDL being
accepted, int64 overflow escaping as `OverflowError`, overlong digits escaping as
Python's integer-conversion `ValueError`, malformed account-note IDs being ignored,
and an unfinished migration being silently marked complete. The two already-safe
spaced literal and quoted-identifier cases remained passing controls in the first
RED run; the final suite tightens both to exact no-whitespace spoof tokens.

### Fix-round GREEN evidence

```text
python -m pytest backend/tests/xhs/test_schema_migration.py -k "ddl_validator or comment_spoof or invalid_historical_ids or half_migration_with_new_ddl" -q --tb=short
14 passed, 11 deselected in 1.98s

python -m pytest backend/tests/xhs/test_schema_migration.py -q
25 passed in 3.77s

python -m pytest backend/tests/xhs/test_schema_migration.py backend/tests/analysis/test_account_note_grounding.py -q
50 passed in 7.11s

python -m pytest backend/tests/analysis backend/tests/xhs -q
388 passed, 1 skipped in 18.72s

python -m pytest backend/tests -q
980 passed, 1 skipped, 32 warnings in 101.65s
```

The skip and 32 warnings remain the pre-existing live opt-in gate and Python 3.12
SQLite datetime-adapter deprecations. `python -m compileall -q backend/app
backend/tests` and `git diff --check` exited 0.

### Fix-round self-review and boundary

- The tokenizer is deliberately bounded to persisted SQLite `CREATE TABLE` DDL;
  it is not a general SQL execution parser. The existing full evidence-schema
  validator still verifies all columns, checks, foreign keys, indexes and triggers.
- The maximum legal historical ID is accepted even though it intentionally
  exhausts future SQLite row IDs rather than permitting identity reuse.
- Citations outside this database remain undiscoverable by a local migration.
- Provider-neutral analysis behavior, note trust-chain checks, the exact shop N/N
  opportunity gate, Task 5, Bailian media, platform writes and untracked research
  remain unchanged. The authenticated live boundary remains `not_run`.

## Fix round 3/5 — canonical physical note-row IDs

### Review finding closed

- Fresh `xhs_account_notes` tables now enforce the named physical constraint
  `ck_xhs_note_canonical_id: id BETWEEN 1 AND 9223372036854775807` while retaining
  `INTEGER PRIMARY KEY AUTOINCREMENT`.
- An independent `xhs_account_note_canonical_id_v3` marker controls the upgrade.
  Marker-present startup is validation-only and rejects a missing/weakened CHECK,
  poisoned rows, or a leftover migration table without repair.
- Marker-absent startup performs every row, v2 identity, history, sequence and
  half-migration check before rebuilding. Canonical legacy rows retain their exact
  IDs, and the previous `sqlite_sequence` high-water mark is restored after the
  CHECK-bearing rebuild. Populated v2 tables containing ID `0` or mixed `-1, 2`
  fail closed with DDL, marker, rows and sequence unchanged.
- Discovery skips any noncanonical persisted note row even if check enforcement is
  bypassed after startup. Evidence resolution uses a bounded canonical SQLite-ID
  parser before every database lookup, and trusted-note verification independently
  rejects a noncanonical ORM row. `account-note:0` and `account-note:-1` therefore
  never reach the model, including through a deliberately unvalidated internal
  request object.
- The older v1 evidence validator accepts only its original exact CHECK set or that
  set plus the exact named v3 CHECK. This preserves v1/v2 migration compatibility
  without allowing arbitrary extra constraints to satisfy v3.

### Fix-round RED evidence

Before production changes, the selected fresh/marker/preflight/migration/analysis
regressions produced:

```text
10 failed, 50 deselected
```

They demonstrated missing fresh CHECK/marker, successful explicit inserts at ID
`0` and `-1`, marker-present old DDL being accepted, populated noncanonical v2
data being certified, no v3 upgrade/sequence-preservation path, a leftover half
migration being ignored, and discovery exposing trusted `account-note:0/-1` rows.

A self-review regression for a present v3 marker plus a leftover half-migration
table then produced an independent `1 failed`; strict marker validation initially
verified only the main table and missed the stale migration table.

### Fix-round GREEN evidence

```text
python -m pytest backend/tests/xhs/test_schema_migration.py backend/tests/analysis/test_account_note_grounding.py -q
61 passed in 9.96s

python -m pytest backend/tests/analysis backend/tests/xhs -q
399 passed, 1 skipped in 22.53s

python -m pytest backend/tests -q
991 passed, 1 skipped, 32 warnings in 109.24s
```

The skip remains the explicit opt-in live gate. The warnings remain the existing
Python 3.12 SQLite datetime-adapter deprecations in content tests.
`python -m compileall -q backend/app backend/tests` and `git diff --check` exited
0 after the final changes.

### Fix-round self-review and boundary

- v3 never assumes SQLite DDL rollback. Known data, history, sequence, physical
  schema and half-migration failures are rejected during read-only preflight.
- A valid sequence higher than all current rows and persisted citations is retained,
  preventing a CHECK-only rebuild from lowering the permanent identity floor.
- The physical CHECK is the primary invariant; discovery, request parsing and
  trusted-note resolution are defense-in-depth for post-startup tampering.
- Provider-neutral analysis, the full note/artifact ownership chain, exact shop N/N
  opportunity gating, Task 5, Bailian media and untracked research remain unchanged.
  The authenticated live boundary remains `not_run`.

## Fix round 4/5 — unified interrupted identity-migration detection

### Review finding closed

- The v2 and v3 migrations now share one fixed set of every XHS note-identity
  temporary table used by the repository:
  `xhs_account_notes_identity_v1` and
  `xhs_account_notes_canonical_id_v2`. One read-only detector is the only source
  used to recognize these leftovers.
- Startup checks that shared set before marker validation or any schema creation.
  A normal three-marker database containing either leftover therefore fails with
  an explicit interrupted-migration error instead of authenticating the database.
- Both v2/v3 marker validators, both marker-absent preflights and the rebuild entry
  point reject either leftover. Marker-present paths remain validation-only;
  marker-absent paths never drop the table, repair schema, advance sequence or
  write a replacement marker when any leftover exists.
- Regression snapshots include the v1/v2/v3 marker rows, main DDL and rows,
  historical citations, `sqlite_sequence`, both temporary-table DDL definitions
  and their row contents. Repeated validator, preflight and real startup attempts
  must leave that complete snapshot byte-for-value unchanged.

### Fix-round RED evidence

Before production changes, the new shared-leftover regression group produced:

```text
python -m pytest backend/tests/xhs/test_schema_migration.py -k "every_known_leftover" -q --tb=short
5 failed, 3 passed, 34 deselected in 1.60s
```

The failures proved that the v2 marker validator ignored both temporary tables,
the v2 marker-absent preflight ignored the v3 temporary table, and the v3 marker
validator/preflight ignored the v2 temporary table. The three passing cases were
the previous one-migration-only controls.

### Fix-round GREEN evidence

```text
python -m pytest backend/tests/xhs/test_schema_migration.py -k "every_known_leftover" -q --tb=short
8 passed, 34 deselected in 1.52s

python -m pytest backend/tests/xhs/test_schema_migration.py -q --tb=short
42 passed in 6.77s

python -m pytest backend/tests/xhs/test_schema_migration.py backend/tests/analysis/test_account_note_grounding.py -q
69 passed in 10.81s

python -m pytest backend/tests/analysis backend/tests/xhs -q
407 passed, 1 skipped in 23.94s

python -m pytest backend/tests -q
999 passed, 1 skipped, 32 warnings in 109.25s
```

The skip remains the explicit opt-in live gate. The warnings remain the existing
Python 3.12 SQLite datetime-adapter deprecations in content tests.
`python -m compileall -q backend/app backend/tests` and `git diff --check` exited
0 after the round-4 production and test changes.

### Fix-round self-review and boundary

- The detector recognizes only the two audited migration implementation names;
  ordinary XHS evidence tables and test-only downgrade names are not classified as
  interrupted production migrations.
- A missing v3 marker plus an `xhs_account_notes_identity_v1` leftover is exercised
  through both the direct v3 preflight and two real `Database(path)` restarts. The
  marker remains absent and the leftover remains intact after every rejection.
- Fresh initialization, legal v1-to-v2 and v2-to-v3 upgrades, permanent ID floors,
  canonical CHECK enforcement, historical citations and sequence preservation are
  unchanged and remain covered by the complete schema suite.
- Provider-neutral analysis, account-note trust, exact shop N/N gating, Task 5,
  Bailian media and untracked research remain unchanged. The authenticated live
  boundary remains `not_run`.
