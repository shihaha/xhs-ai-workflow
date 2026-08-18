# Task 8R-3 report: request-time cleanup integration

## Scope

This task only connects the approved quarantine service to Task 8 material and
package business paths. It does not add the cleanup worker or API (8R-4), close
the approved ZIP/model-order minors (8R-5), or claim any live environment test.

## RED evidence

- Initial `test_quarantine_integration.py`: 5 failed. Material failures produced
  no cleanup fact, package reservations had no build token, and a builder-token
  takeover incorrectly finalized as ready.
- Transaction-uncertainty regression: 1 failed because an ambiguous failed-CAS
  acknowledgement replaced the original build error and did not classify the
  cleanup fact as uncertain.
- Superseded direct-delete tests: 5 failed after the physical-delete import was
  removed or because they still expected immediate file removal.

## Implementation

- `ContentService` owns an injected `ArtifactCleanupService` (with a compatible
  default until Task 8R-4 wires the app-owned instance).
- Material write/commit failures roll back the business session, retain the
  managed file and enqueue `material_persistence_failed`. A commit acknowledgement
  ambiguity is deliberately allowed to become `needs_human/live_reference` when
  the persisted material is later processed.
- Removed the service's `_cleanup_unpersisted_material`,
  `_cleanup_unreferenced_artifact`, Windows reference scan and direct physical
  deletion calls.
- New and replacement package reservations receive a canonical UUID build token.
  Pre-build checks, success CAS and failure CAS bind package ID, content item ID,
  revision ID, `building` status, exact path and build token.
- The ready-package update and content-item `exported` update remain in one SQLite
  transaction. A lost builder cannot fail or overwrite the winner; its produced
  bytes remain in place and become `package_builder_lost` cleanup work.
- Failed builders persist the observed artifact hash and size with the exact-CAS
  failure state before enqueue. An ambiguous failure-commit acknowledgement is
  classified `package_transaction_unknown`, keeps the original exception, retains
  the file and requires the cleanup service to re-prove ownership later.
- Re-exported corrupt artifacts are retained and queued as `package_replaced`.
  Old round 2/4/5 tests were updated only where their immediate-delete expectation
  was replaced by the approved design; concurrency, ownership and no-unlink
  assertions remain.
- Database startup recovery was rechecked: it already atomically changes only
  interrupted `building` rows to `failed`, preserves build-token/path/identity
  fields and inserts idempotent cleanup facts without filesystem deletion.

## Verification

```text
python -m pytest backend/tests/content/test_quarantine_integration.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q
38 passed in 4.98s

python -m pytest backend/tests/content -q
207 passed in 24.11s

python -m pytest backend/tests -q
520 passed, 1 skipped in 38.99s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compile and diff checks exited 0. A source search found no
`remove_contained_regular` or `.unlink(` use in `content/service.py` or `db.py`.
The Git output contained only existing Windows LF/CRLF notices.

Live Bailian remains `not_run: BAILIAN_API_KEY unavailable`; Android remains
`not_run: device unavailable`; seven-day UAT remains `not_run`. Task 8R-3 still
requires independent review before acceptance.

## Fix round 1/5: durable reservation outbox

Independent review found two Important gaps: business failure paths still relied
on a second best-effort database write after bytes existed, and commit
acknowledgement ambiguity was always reported as failure even when the exact
business transaction had committed.

RED evidence:

- Five new outbox tests failed because no caller-session enqueue/cancel interface
  existed, reservation failure did not stop the managed write or builder, and no
  pending fact was visible before either material or package bytes were written.
- Two active-writer tests failed because reservations were immediately due and a
  future worker could cross the unfinished business write.
- Ten older tests exposed assumptions superseded by durable cancelled audit rows,
  explicit `transaction_unknown`, or the new pre-reservation archive build.

Implementation:

- Added `ArtifactCleanupService.enqueue_in_session()` and exact idempotent
  `cancel_in_session()`. Neither commits; the public `enqueue()` reuses the same
  insert implementation and remains backward compatible.
- Material paths now persist a `material_write_reserved` pending fact before the
  managed write. Material insert and exact cancellation commit together. A fresh
  read proves ID, product, logical identity, version, path, hash, size, media type,
  kind and cancelled cleanup after ambiguous acknowledgement; otherwise the file
  and pending reservation remain and the service raises explicit
  `transaction_unknown`.
- Package bytes are built deterministically in memory before filesystem
  reservation, so the pending `package_build_reserved` fact binds the final hash
  and size. New/replacement building row and build cleanup commit together;
  replacement old-path cleanup is inserted in that same transition transaction.
- Ready package, exported item and exact build-cleanup cancellation share one
  transaction. A fresh read proves package/item/revision/path/token/status/hash/
  size and cancelled cleanup before returning success after an ambiguous commit.
  Failed acknowledgement is accepted only when the exact failed builder and its
  pending cleanup are freshly visible. CAS loss never mutates the winner.
- Active material/package reservations are due after 24 hours. This prevents the
  future 8R-4 worker from claiming a path between reservation and business commit;
  failed/crashed work remains durably recoverable after the bounded delay.
- Startup recovery continues to fail interrupted building rows and reuses an
  existing open build reservation; it inserts one pending recovery fact only when
  the historical row lacks one. Cancelled success audit rows are preserved.

Final verification:

```text
python -m pytest backend/tests/content/test_quarantine_integration.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q
45 passed in 6.63s

python -m pytest backend/tests/content -q
214 passed in 27.63s

python -m pytest backend/tests -q
527 passed, 1 skipped in 44.65s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compile and diff checks exited 0. A source search found no request/startup physical
delete and no standalone `cleanup_service.enqueue()` call in the content business
service. Git emitted only the repository's Windows LF/CRLF notices. Bailian,
Android and seven-day UAT remain `not_run`; the fix awaits independent re-review.

## Fix round 2/5: exact reservation/failure transaction outcomes

Independent review found that package reservation commit acknowledgement was not
classified before the filesystem write, and that an explicit failure could leave
an otherwise healthy pending reservation at its active-writer 24-hour delay.

RED evidence:

- Package reservation acknowledgement tests covered exact committed landing,
  no landing, partial/mismatched landing and an unreadable fresh-proof database.
- Failed finalizer tests covered no landing and a committed package whose cleanup
  identity no longer matched.
- A startup regression with an existing pending cleanup due 24 hours later failed:
  the old recovery reused the row without advancing `not_before`.

Implementation:

- Reservation acknowledgement uses a typed three-way outcome. A fresh session
  proves package ID, item ID, revision ID, `building` status, path, canonical
  build token, placeholder hash/size and the exact pending cleanup ID, owner,
  path, hash, size and due time. A proven landing continues without creating a
  duplicate reservation; proven no-landing stops before writing; partial,
  mismatched or unreadable facts raise `package_reservation_transaction_unknown`.
- Failed finalization writes the exact candidate hash/size and advances the exact
  pending cleanup to fresh-now in the package `failed` transaction. Its typed
  outcome is `failed`, `proven_failed`, `unknown` or `lost`; only an exact fresh
  failed-package plus pending-cleanup proof accepts an acknowledgement loss.
  Unknown replaces the original build exception with
  `package_failure_transaction_unknown`; lost ownership preserves the original
  concurrency/build error and does not modify the winner.
- Explicit material write failure likewise advances only its exact pending
  reservation in one transaction. If that due-state commit acknowledgement is
  uncertain, a fresh exact proof is required; otherwise the service reports
  `material_failure_transaction_unknown` and retains the file/fact.
- Startup recovery now inserts or reuses the exact pending interrupted-package
  cleanup and advances it to fresh-now in the same SQLite transaction that marks
  the package failed. An identity mismatch fails closed for isolated migration.
  Normal active material/package reservations remain protected for 24 hours.

Final verification:

```text
python -m pytest backend/tests/content/test_quarantine_integration.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py backend/tests/content/test_artifact_cleanup_schema.py -q
119 passed in 13.77s

python -m pytest backend/tests/content -q
221 passed in 25.35s

python -m pytest backend/tests -q
534 passed, 1 skipped in 41.55s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compile and diff checks exited 0. Source searches found no
`remove_contained_regular`, `.unlink(` or standalone `cleanup_service.enqueue()`
in the content request/startup paths. Git emitted only the repository's Windows
LF/CRLF notices. Bailian, Android and seven-day UAT remain `not_run`; this round
awaits independent re-review and does not begin 8R-4.

## Fix round 3/5: lost-builder and material persistence classification

Independent review found that a lost package builder was still wrapped as an
ordinary unsafe/build failure, material database failures could remain delayed
despite a proven non-landing, and replacement rollback proof ignored a partial
replacement-cleanup fact.

RED evidence:

- Generic finalizer takeover and unsafe-path-after-takeover both failed to emit
  a stable lost-builder state conflict.
- Proven material non-landing, writable unknown and database-unreadable cases
  all retained the old undifferentiated `transaction_unknown` behavior.
- A replacement rollback with the build cleanup absent but a replacement cleanup
  ID present was incorrectly classified as fully not landed.

Implementation:

- Both package failure catches explicitly handle `_FailureOutcome.LOST` as
  `package_builder_lost`. The failed CAS has already rolled back, so neither the
  winner package nor its outbox row changes. `ContentStateError` continues through
  the existing API translator as HTTP 409; unsafe-path failures are not converted
  to 422 after ownership has been lost.
- Material commit acknowledgement now produces `LANDED`, `NOT_LANDED` or
  `UNKNOWN` from a fresh exact material/outbox read. Landed returns the persisted
  material only with its cancelled cleanup. Not-landed requires no material row
  and the exact pending reservation, then advances it to fresh-now and records
  `material_persistence_failed` before raising an explicit state error.
- Writable unknown likewise uses an exact pending-row CAS to persist
  `material_transaction_unknown` and fresh-now. If the database cannot be read or
  written, no due-now claim is made: the pre-existing 24-hour reservation remains
  durable and the error states that persistence/cleanup due state is unknown.
- Replacement reservation rollback is not considered absent unless both the
  build-cleanup ID and the replacement-cleanup ID are freshly absent. Either
  partial row produces `package_reservation_transaction_unknown`; a regression
  also proves the fully absent pair remains `package_reservation_failed`.

Final verification:

```text
python -m pytest backend/tests/content/test_quarantine_integration.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py backend/tests/content/test_artifact_cleanup_schema.py -q
125 passed in 16.67s

python -m pytest backend/tests/content -q
227 passed in 30.13s

python -m pytest backend/tests -q
540 passed, 1 skipped in 48.44s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compile and diff checks exited 0. Source searches found no request/startup direct
delete and no standalone `cleanup_service.enqueue()` business-path call. Git
emitted only Windows LF/CRLF notices. Bailian, Android and seven-day UAT remain
`not_run`; this round awaits independent re-review and does not begin 8R-4.

## Fix round 4/5: immutable package-generation cleanup provenance

The remaining review finding had one root cause: a cleanup fact identified a
package row, but not the exact builder generation that created its bytes. A
retry could reuse the package ID with a new token and path, causing the old
cleanup to be mistaken for the current package or rejected as an owner mismatch.

RED evidence:

- Fresh schema and direct-SQL tests failed because no `source_build_token`
  column, typed constraint, exact insertion guard or immutability guard existed.
- A real ready-package retry produced an old `package_replaced` cleanup with no
  durable generation proof.

Implementation:

- Added nullable `source_build_token` to cleanup facts. Material cleanups must
  keep it NULL; package cleanups require a canonical UUID.
- A retry-safe marked migration rebuilds the cleanup table with the physical
  CHECK, backfills only exactly provable legacy package generations, recreates
  all guards, and fails closed when historical provenance cannot be proven.
- Package cleanup INSERT requires an in-transaction exact package match over ID,
  Windows-equivalent path, SHA-256, size and build token. The source token is
  frozen after insertion and physical schema, trigger definitions and data are
  revalidated at startup.
- Replacement cleanup is inserted while the old generation is still current;
  the package row is then reserved with the new token/path in the same
  transaction. Build cleanup records bind the new token.
- Commit-ack proof includes the persisted source token and accepts an existing
  reservation whose persisted `not_before` is no later than the new candidate,
  instead of requiring a newly generated timestamp.
- Reference classification treats exact current-generation bytes as the source,
  treats a different current token/path as an authorized historical artifact,
  and continues to fail closed for same-token identity mismatches or conflicting
  live paths. Windows case/NFC equivalence remains shared with the SQL guards.
- Concurrency at old-generation insertion is translated to a stable content
  state conflict; no request/startup physical deletion was added.

Verification:

```text
python -m pytest backend/tests/content -q
228 passed

python -m pytest backend/tests -q
541 passed, 1 skipped

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only Windows LF/CRLF notices.
Bailian remains `not_run: BAILIAN_API_KEY unavailable`, Android remains
`not_run: device unavailable`, and seven-day UAT remains `not_run`. This round
does not begin 8R-4 and requires independent re-review.

## Fix round 5/5: status-bound current generation and migration proof

The final review found two related trust gaps. The cleanup classifier treated an
exact current package generation as disposable regardless of package status, and
the marker-absent source-token migration accepted any canonical token without
proving it against the current package identity.

RED evidence:

- Exact current `building` and `ready` generations were moved to quarantine;
  a quarantined failed generation that became `ready` was physically deleted.
- A forged but canonical source token and a cleanup whose package had moved to a
  different generation were both accepted when the source-token marker was
  absent.
- Legacy NULL provenance required an explicit safe-backfill contract.

Implementation:

- Python classification now treats the exact package ID, Windows-equivalent
  path, SHA-256, size and source token as a cleanup source only while the package
  is `failed`. The same current generation in `building` or `ready` is a
  `live_reference` before move and again at final-delete authorization.
- The shared SQL exact-owner exemption also requires `status='failed'`. A
  current nonfailed package may therefore support the durable moved
  `needs_human/live_reference` fact, while an exact failed source still cannot
  fabricate that exception. Existing old-generation cleanup remains authorized
  only after the package has a different token and different path.
- Before any marker-absent DDL, backfill, trigger recreation or marker write, the
  migration proves every existing package cleanup against current package ID,
  Windows-equivalent path, SHA-256, size and canonical build token. Existing
  non-NULL source tokens must equal that exact token; NULL is backfilled only
  from that exact match. Forged, unmatched and half-migrated provenance fails
  closed without repairing the source column, marker or guards.
- A valid half migration restores the two source-token guards and marker. The
  marked path remains validation-only.

Final verification:

```text
python -m pytest backend/tests/content/test_quarantine_integration.py backend/tests/content/test_artifact_cleanup_service.py backend/tests/content/test_artifact_cleanup_schema.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q
174 passed

python -m pytest backend/tests/content -q
236 passed

python -m pytest backend/tests -q
549 passed, 1 skipped

python -m compileall -q backend/app backend/tests
git diff --check
```

Compile and diff checks exited 0. The content request and startup paths still
contain no direct physical-delete call. Bailian remains
`not_run: BAILIAN_API_KEY unavailable`, Android remains
`not_run: device unavailable`, and seven-day UAT remains `not_run`. This is the
fifth and final 8R-3 fix round; it does not begin 8R-4 and requires independent
re-review.
