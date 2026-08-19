# Stabilization S2B report — append-only account evidence and post-model revalidation

Date: 2026-08-19

## Scope and outcome

S2B closes the account-note evidence lifetime and analysis TOCTOU findings from
the whole-plan C3 review. Account collection is now append-only and versioned;
an older canonical `account-note:<id>` continues to resolve against the exact
collection snapshot and formal artifact that created it. The current account
API continues to return only the newest snapshot, so the frontend contract and
UI do not need a new version selector.

Analysis creation now performs the complete trust resolution both before and
after the model call. The second resolution and the success/opportunity insert
share one SQLite writer reservation. A changed, missing or no-longer-trusted
evidence set is durably recorded as a generic non-success with no output and no
opportunities. The existing complete-shop-evidence N/N eligibility gate is
unchanged.

This work does not change Bailian behavior or the pre-existing untracked
`research/` tree.

## RED-first evidence

The first executable regression selection was run before implementation:

```text
python -m pytest backend/tests/analysis/test_account_note_grounding.py \
  -k "recollection_never or concurrent_recollection or artifact_drift_while_model" -q
3 failed, 56 deselected in 1.17s
```

The failures proved three independent defects:

- a normal recollection deleted the old canonical `account-note:1` row;
- a concurrent recollection made the model's original citation unresolvable;
- changing the formal artifact while the model was blocked still allowed a
  `succeeded` analysis commit.

The identical selection became GREEN after the mechanism changes:

```text
3 passed, 56 deselected in 1.22s
```

A separate post-model note-row drift case was added while hardening the same
boundary. The final dedicated S2B suite is `15 passed in 4.38s`.

## Immutable collection identity and latest view

- `xhs_account_profiles` is retained as an immutable first-seen account anchor,
  preserving the legacy account identity and its original provenance.
- Each completed collection appends one
  `xhs_account_profile_snapshots` row. Its canonical integer `id` is the
  snapshot/version identity; collection job and artifact are independently
  unique and physically foreign-keyed.
- Every note observation is a new canonical `xhs_account_notes` row bound to
  that collection job, artifact and raw digest. The uniqueness boundary is
  `(note_id, user_id, collection_job_id)`, so recollection never overwrites an
  older observation.
- `xhs_account_snapshot_notes` binds each note row to exactly one snapshot and
  records the artifact's exact zero-based order.
- Unconditional UPDATE and DELETE triggers freeze the anchor, snapshots, note
  rows and memberships. Insert-time triggers enforce job/artifact/user binding.
- Current profile and note reads explicitly select the greatest snapshot
  identity for the account and follow its ordered membership. Historical
  analysis lookup follows the cited note row back to its own immutable snapshot
  rather than to the current view.
- Retrying the same collection job is idempotent only when every normalized
  profile/note value matches exact canonical JSON. It reuses the snapshot and
  canonical note IDs with zero row growth. A conflicting retry fails closed.
  Concurrent distinct jobs serialize naturally in SQLite and append distinct
  snapshots; the latest committed snapshot is the current view.

## Retry-safe physical migration

The new `xhs_account_snapshot_evidence_v5` migration is restart-safe and
fail-closed:

- With the marker present, startup performs validation only; it does not repair
  tables, indexes, triggers or data.
- Validation covers exact columns/types/nullability, canonical-ID checks,
  foreign keys and `RESTRICT` actions, unique constraints, indexes, binding and
  immutability trigger SQL, case-insensitive migration leftovers, membership
  completeness/order, snapshot-parent provenance and exact artifact bytes/raw
  digests.
- A coherent legacy v4 database is rebuilt transactionally while preserving
  every existing canonical note ID, SQLite sequence and the old profile/note
  provenance. One snapshot and ordered membership are backfilled from each
  legacy account collection.
- An empty half-created v5 shape can be discarded and recreated. A populated
  partial/ambiguous shape, a tampered certified shape or any recognized
  temporary leftover fails before certification and without data repair.

Coverage includes a real physical v4 DDL image, marker-present validation-only
startup, retry, empty-half recovery, populated-half rejection, mixed-case
leftovers, physical UPDATE/DELETE attempts on all four layers, canonical-ID
preservation and historical analysis resolution after migration.

## Analysis post-model trust fence

Every requested evidence ID is resolved fully before the model call. The trust
fingerprint includes the public facts, sorted account scope, exact ordered
allowed IDs and immutable provenance needed by each evidence kind. For an
account note this includes the canonical note ID, snapshot ID and position,
complete ordered membership, job/artifact identity and metadata, committed
journal identity, file device/inode/size/mtime, formal SHA-256 and every
profile/note raw digest.

After the model returns, analysis opens `BEGIN IMMEDIATE`, resolves the complete
evidence set again, compares fingerprint/scope/allowed IDs, and only then
inserts the successful analysis and opportunities in that same transaction.
Loss, scope change, artifact replacement/tamper, row drift or membership drift
produces `needs_human` with `evidence_changed_after_model`, generic safe detail,
null output and zero opportunities. Provider exception text is not exposed by
this path. The model response is never allowed to authorize a citation outside
the original exact allowed-ID list.

## API and frontend compatibility

The account profile and note response shapes are unchanged. Their producer now
uses the latest explicit snapshot internally, and analysis evidence discovery
lists only notes belonging to latest snapshots. Existing historical analysis
records and direct historical citations remain resolvable. No frontend source
or new UI was needed; the existing account/radar flow received full frontend
and controlled E2E regression coverage.

## Controlled verification

- Dedicated S2B snapshot/migration suite: `15 passed`.
- Final targeted retry/recollection/model-drift selection: `5 passed,
  70 deselected`.
- Schema migration suite: `63 passed`.
- Complete XHS + analysis suites: `679 passed, 2 skipped`.
- Frontend unit tests: `47 passed`; production TypeScript/Vite build passed.
- Controlled fresh-runtime E2E repeat gate: `5 passed` with
  `--repeat-each=5`.
- Final `scripts/verify.ps1`: exit 0; backend `1274 passed, 2 skipped` in
  222.78s; Python compile passed; frontend `47 passed`; production build
  passed; controlled E2E `1 passed`; npm audit found `0 vulnerabilities`;
  tracked-secret and release-boundary scans passed.
- Diff/compile/scope scans passed. Production XHS/analysis code contains no
  delete-and-reinsert account fact path. The staged file list contains no
  Bailian, frontend or `research/` file.

## Live status

Real authenticated XHS execution remains exactly:

```text
not_run
```

The guarded contract result is `2 passed, 1 skipped`; the skip says
`not_run: XHS_LIVE_TEST=1 was not supplied`. No real account/search success or
identity is claimed. Controlled adapters, migrations and E2E evidence verify
the software behavior only.

Independent post-commit review remains pending.
