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
