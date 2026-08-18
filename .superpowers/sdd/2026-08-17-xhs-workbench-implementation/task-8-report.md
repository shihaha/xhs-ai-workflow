# Task 8 implementation report

## Scope delivered

- Added database-backed products bound to a persisted successful opportunity.
- Added immutable, system-hashed product material versions. Input files must be contained regular runtime files; the accepted bytes are copied into a managed immutable runtime path.
- Added source-linked research, model-generated immutable revisions, append-only reject/regenerate/approve decisions, and the finite item states `research`, `draft`, `review`, `rejected`, `approved`, `exported`.
- Added product, material, content-item, review, regenerate, export and content-package HTTP routes under `/api/v1`.
- Added byte-deterministic ZIP export with sorted entries, fixed timestamps and permissions, canonical JSON, final text, cited research/claims, review history, product metadata, approved material bytes and system-computed SHA-256 values.
- Export remains local and explicitly records `automatic_publish: false`; no publishing integration was added.
- Added two versioned tutorial-derived structural seeds (`list-v1` and `problem-solution-v1`) without importing any tutorial demo business record.

## Tutorial material read

- `系统地图.md`
- `agents/主代理调度.md`
- `agents/日更生成.md`
- `agents/日更内容审查.md`
- `01-产品/演示产品一/资料索引.md`
- `模板A-清单型/模板说明.md` and `SKILL.md`
- `模板C-问题解决型/模板说明.md` and `SKILL.md`

Only the required product isolation, explicit material reference, generate-review-return/approve flow, structural template guidance and no-publish boundary were adapted.

## TDD evidence

RED command:

```text
python -m pytest backend/tests/content -q
```

Observed RED: exit code 1 during collection with three `ModuleNotFoundError: No module named 'backend.app.features.content'` errors (`test_workflow.py`, `test_review.py`, `test_export.py`).

Focused GREEN:

```text
python -m pytest backend/tests/content -q
18 passed in 1.55s
```

Full backend GREEN:

```text
python -m pytest backend/tests -q
331 passed, 1 skipped in 13.10s
```

The skipped test is the existing opt-in live Bailian contract. `BAILIAN_API_KEY=unavailable`, so no live model verification is claimed.

Additional verification:

```text
python -m compileall -q backend/app backend/tests
git diff --check
```

Both completed with exit code 0 (Git emitted only the repository's Windows LF/CRLF notices for `db.py` and `main.py`).

## Controlled E2E and failure coverage

- A fresh temporary SQLite database with a controlled fake `ModelAdapter` completes opportunity/evidence fixture → product → material → content revision → approval → deterministic package over the HTTP API.
- Missing model configuration returns HTTP 503 and leaves the content-item list empty.
- Invalid structured output, unknown/cross-scope citations, foreign materials, illegal state changes, export before approval, modified managed material bytes, input/output symlinks, traversal and ZIP case collisions do not produce successful output.
- Provider success metadata is allowlisted; arbitrary headers/tokens/usage keys do not reach the database response.

## Live status and residual risk

- Bailian live generation: `not_run: BAILIAN_API_KEY unavailable`.
- Human review and package export were exercised with controlled persisted fixtures, not real Xiaohongshu production content.
- Real operator material choices, real model quality and the seven-day UAT remain Task 10/user-supplied live acceptance work.

## Fix round 1: review and trust hardening

RED was recorded with:

```text
python -m pytest backend/tests/content/test_hardening.py -q
```

The new hardening module failed collection because the required strict `RegenerateCreate` CAS request did not exist.

Implemented review findings:

- Review uses `expected_revision_id` and a conditional database update; a partial unique index allows only one terminal approve/reject per revision.
- Regeneration reserves the rejected revision with persisted `draft` state before calling the model. Concurrent/stale calls conflict, and model failure restores `rejected` while retaining the append-only regeneration attempt.
- Opportunity/analysis/evidence/material trust is revalidated before and after model execution and before review/export. Persisted opportunity rows must match validated analysis output; artifact citations must still pass Task 7 trusted shop provenance.
- Every item has 1-20 ordered real output images, first-image cover identity, a complete model image plan and one concrete passing human visual check per image before approval.
- Materials use NFC-safe Windows filenames, detected supported media bytes, managed immutable copies, count/aggregate caps and derived live availability.
- ZIP exports use ordered `images/01.ext...`, fixed metadata, NFC+casefold collision checks, entry/uncompressed/compression-ratio caps, manifest image/cover/material hashes, and no publish action.
- Package creation reserves a persisted `building` row before filesystem work; concurrency produces one builder, failures never become ready, startup changes stranded builders to `failed`, and GET requires a verified ready hash while list projections expose missing/corrupt/building/failed.
- Runtime writes create an empty final-file handle first, verify its identity and resolved containment, then write through that same handle. The parent-swap regression proves no payload is written outside the runtime before containment verification.
- Task 8 schema startup validation now checks required columns/constraints/indexes, marks a migration, rebuilds only empty legacy tables and fails closed on non-empty legacy/malformed tables.

Fix-round GREEN:

```text
python -m pytest backend/tests/content -q
41 passed in 3.54s

python -m pytest backend/tests/analysis backend/tests/test_jobs_api.py backend/tests/test_job_state_machine.py backend/tests/test_jobs_hardening.py -q
118 passed, 1 skipped in 6.29s

python -m pytest backend/tests -q
354 passed, 1 skipped in 16.80s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0. The single skipped test remains the opt-in Bailian live contract because `BAILIAN_API_KEY` is unavailable; no live model claim is made.

## Fix round 2: package, image and schema integrity

The eight Important review groups were first captured in `test_round2_hardening.py`.

Initial RED:

```text
python -m pytest backend/tests/content/test_round2_hardening.py -q
13 failed in 1.46s
```

An additional predicate-definition probe was then observed RED before its minimal fix:

```text
python -m pytest backend/tests/content/test_round2_hardening.py::test_schema_validator_rejects_wrong_terminal_index_predicate_with_data -q
1 failed in 0.36s
```

Implemented review findings:

- Regeneration now restores its CAS-reserved item to `rejected` when trust changes after the model returns, so the prior revision remains recoverable instead of being stranded in `draft`.
- PNG, JPEG and WebP materials are fully verified and decoded with Pillow, including declared-format matching and bounded dimensions; marker-valid malformed images are rejected.
- Re-export of a failed, missing or corrupt package uses a conditional row reservation so only one builder wins, rotates the target path, and safely removes the no-longer-referenced contained artifact.
- Task 8 startup validation now checks the complete required column/nullability set, named CHECK definitions, foreign-key column mappings, composite/unique constraints and the exact terminal-review partial unique-index definition. Empty legacy schemas are rebuilt; populated malformed schemas fail closed.
- Package availability uses the same independent 250 MiB package bound as deterministic ZIP construction instead of the 50 MiB per-material bound.
- Startup recovery removes only verified contained regular artifacts for stranded `building` rows, leaves unsafe/untrusted targets untouched, and persists those rows as failed.
- Exported review history includes per-image `visual_checks`; the complete ordered image plan is exported as `content/image-plan.json` and covered by the manifest entry hashes.
- Material names reject the full Windows reserved-device set used by this workflow, C0/DEL/C1 controls, unsafe punctuation and trailing dot/space. NFC+casefold-equivalent names share a single version sequence through a persisted logical key.

Round-2 GREEN and verification:

```text
python -m pytest backend/tests/content/test_round2_hardening.py -q
14 passed in 1.79s

python -m pytest backend/tests/content -q
55 passed in 5.38s

python -m pytest backend/tests/analysis backend/tests/test_jobs_api.py backend/tests/test_job_state_machine.py backend/tests/test_jobs_hardening.py -q
118 passed, 1 skipped in 6.20s

python -m pytest backend/tests -q
368 passed, 1 skipped in 17.76s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only the repository's Windows LF/CRLF notices. The live Bailian/model and Android-device checks remain `not_run` because no live key/device was supplied; this round makes no live-provider or real-device claim.

## Fix round 3: runtime-root and artifact race closure

The two Critical and four Important review groups were captured before production changes. The two approved Minor findings remained deferred.

Initial RED:

```text
python -m pytest backend/tests/content/test_round3_hardening.py -q
13 failed, 3 passed in 0.94s
```

The schema fixtures were then made independent of the terminal-review index so all malicious definitions failed for their intended reason:

```text
python -m pytest backend/tests/content/test_round3_hardening.py -q -k schema
3 failed in 0.49s
```

A final Windows trim-equivalence probe was also observed RED before normalizing device-name stems:

```text
python -m pytest backend/tests/content/test_round3_hardening.py -q -k windows_unsafe_entry_names
2 failed, 7 passed in 0.30s
```

Implemented review findings:

- `Database` now receives the configured trusted runtime root explicitly from application startup. Startup recovery never derives artifact paths from the database parent; without an explicit runtime root it conservatively retains files while still failing stranded reservations.
- Windows artifact removal opens the intended regular file with delete access, verifies containment and file identity before and after opening, and applies deletion to that open handle. It never performs a check-then-path-unlink. Parent-swap races either delete only the already-opened intended orphan or fail safely while preserving the outside victim.
- Every step after a package reservation commit, including re-load, trust validation, material loading, build and finalization, is inside one failure boundary. A path-and-status CAS changes only that builder's reservation from `building` to `failed`, enabling a later retry without overwriting a concurrent builder.
- Task 8 schema validation compares complete normalized CHECK definitions, all expected FK local/remote columns and `ondelete` actions, exact unique constraints, and exact index columns/uniqueness/partial predicates. Weak constant-true checks, changed actions and missing direct item FKs fail closed when data exists.
- Image dimensions and the pixel cap are checked before both verification and pixel decode. Pillow decompression-bomb warnings are promoted to rejection and both warning/error forms are mapped to factual material validation failures; valid PNG/JPEG/WebP behavior remains covered by the content suite.
- Deterministic ZIP construction independently rejects Windows device names (including trimmed device-name equivalents), C0/DEL/C1 controls, unsafe punctuation, trailing dot/space and NFC+casefold Windows-equivalent collisions for every path component.

Round-3 GREEN and verification:

```text
python -m pytest backend/tests/content/test_round3_hardening.py -q
18 passed in 0.88s

python -m pytest backend/tests/content -q
73 passed in 6.07s

python -m pytest backend/tests/analysis backend/tests/test_jobs_api.py backend/tests/test_job_state_machine.py backend/tests/test_jobs_hardening.py -q
118 passed, 1 skipped in 6.24s

python -m pytest backend/tests -q
386 passed, 1 skipped in 18.14s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only the repository's Windows LF/CRLF notices. Live Bailian and Android-device validation remain `not_run` because no live key or device was supplied; no live-provider or real-device behavior is claimed.

## Fix round 4: ownership-proven cleanup and literal-safe schema validation

The three confirmed review groups were captured in `test_round4_hardening.py` before production changes.

Initial RED:

```text
python -m pytest backend/tests/content/test_round4_hardening.py -q
5 failed, 2 passed in 1.44s
```

The named-CHECK fixtures were then isolated from unrelated index recreation effects and observed RED for their intended literal comparison behavior:

```text
python -m pytest backend/tests/content/test_round4_hardening.py -q -k schema
3 failed, 4 deselected in 0.48s
```

Implemented review findings:

- Material-version conflict cleanup now uses the same runtime-contained, handle-bound deletion helper as package cleanup. The service no longer performs a path-based unlink after a commit conflict; unsafe or replaced targets are retained.
- Startup recovery proves a stranded artifact path is exclusively owned by its `building` package before deletion: the path must have the expected package/item shape, have exactly one package owner and conflict with no managed material. Ambiguous, material-owned, ready-package-owned and unexpected paths are retained while the stranded row is still marked `failed`.
- SQL definition normalization now removes layout whitespace only outside quoted tokens. String literals and quoted identifiers remain byte-distinct, so named CHECK constraints, the SHA GLOB and the terminal-review partial-index predicate are compared without erasing meaningful literal differences. Populated malformed schemas fail closed.
- Round-3 invariants remain covered by the focused suite: configured runtime separation, handle-bound deletion, post-reservation failure CAS, pre-decode image caps and Windows-safe deterministic ZIP names.

Round-4 GREEN and verification:

```text
python -m pytest backend/tests/content/test_round4_hardening.py -q
7 passed in 1.28s

python -m pytest backend/tests/content -q
80 passed in 7.13s

python -m pytest backend/tests -q
393 passed, 1 skipped in 19.56s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only the repository's Windows LF/CRLF notices. Live Bailian and Android-device validation remain `not_run` because no live key or device was supplied; this round makes no live-provider or real-device claim.

## Fix round 5: canonical artifact ownership and reservation finalization

The final Critical and two Important review groups were captured in
`test_round5_hardening.py` before production changes.

Initial RED:

```text
python -m pytest backend/tests/content/test_round5_hardening.py -q
8 failed, 3 passed in 2.26s
```

The corrected generic-failure probe was also mutation-checked by temporarily
removing the two cleanup calls, then restoring them:

```text
python -m pytest <generic-failure-test> <post-create-path-test> -q
2 failed in 0.58s
2 passed in 0.52s
```

An item-state CAS-loss probe then exposed that the builder's own newly-failed row
prevented cleanup of its unreferenced bytes:

```text
python -m pytest backend/tests/content/test_round5_hardening.py -q -k 'finalizer and item'
1 failed in 0.49s
```

The failure finalizer now reports whether this builder won its exact reservation
CAS. Only in that case is its own failed row excluded from the subsequent
ownership proof; any other material/package owner still forces preservation.

Implemented review findings:

- Startup cleanup now requires canonical lower-case UUID text for both package and
  content-item identities, an exact NFC-normalized package path shape, a trusted
  contained regular artifact, and exclusive ownership under Windows-equivalent
  absolute path keys or physical file identity. Case/NFC-equivalent material paths
  and package paths in every status preserve the artifact while stranded builders
  are still marked failed; links, junctions and abnormal identities never authorize
  deletion.
- Material creation cleanup now covers generic SQLAlchemy failures and post-create
  path-validation failures. A fresh independent connection first proves the new
  material row was not persisted and that no material/package owns an equivalent
  file. Ambiguous commit acknowledgement, lookup failure or an existing owner keeps
  the artifact; cleanup remains handle-bound and the originating exception remains
  the request failure.
- Package finalization now conditionally updates the exact package ID, item ID,
  revision ID, `building` status and reserved path, together with a current-revision
  item CAS in one transaction. Path/status/other-builder takeover cannot become
  `ready` or mark the item exported. A lost reservation safely removes only a newly
  built unreferenced artifact and preserves any artifact claimed by another owner.
- Round-3/4 runtime-root, handle-bound deletion, schema-literal, image-limit and ZIP
  safety invariants remain covered. The two previously approved Minor findings
  remain deferred unchanged.

Round-5 GREEN and verification:

```text
python -m pytest backend/tests/content/test_round5_hardening.py -q
14 passed in 2.81s

python -m pytest backend/tests/content/test_round4_hardening.py -q
7 passed in 1.36s

python -m pytest backend/tests/content/test_round3_hardening.py -q
18 passed in 0.90s

python -m pytest backend/tests/content -q
94 passed in 9.47s

python -m pytest backend/tests -q
407 passed, 1 skipped in 21.53s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only the repository's Windows
LF/CRLF notices.

Live Bailian and Android-device validation remain `not_run` because no live key or
device was supplied. This round makes no live-provider, real-device or seven-day UAT
claim.

## Approved quarantine redesign — Task 1: durable schema and startup recovery

The approved replacement for request/startup-time deletion began with a durable
database boundary. The initial focused run was observed RED before production
changes:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_schema.py -q
8 failed in 1.01s
```

The failures covered the absent cleanup table, package builder token, strict read
schema, migration marker, physical constraints and enqueue-only startup recovery.
A separate timestamp-constraint probe was also observed RED (`1 failed in 0.39s`)
before the database CHECK was added.

Implemented in this task:

- Added `artifact_gc_queue` with strict owner/state/size/hash/path/timestamp/attempt
  CHECK constraints and one partial unique index for open owner/path facts.
- Added nullable `content_packages.build_token` for the exact builder CAS completed
  by later redesign tasks.
- Added `task8_artifact_quarantine_v1`, which is retry-safe when physical DDL exists
  without the marker and validates columns, types, nullability, named CHECKs and the
  exact partial index before trusting the database.
- Startup now changes interrupted `building` packages to `failed` and atomically
  enqueues a `pending` cleanup fact. It never opens, moves or deletes the artifact.
- Replaced the two old startup-deletion regression expectations with the approved
  retain-and-enqueue contract; configured runtime and database-parent files are both
  retained.

Final Task-1 verification:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_schema.py backend/tests/content/test_content_schema_migration.py -q
11 passed in 1.18s

python -m pytest backend/tests/content -q
103 passed in 10.70s

python -m pytest backend/tests -q
416 passed, 1 skipped in 23.68s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only the repository's Windows
LF/CRLF notices. Bailian remains `not_run: BAILIAN_API_KEY unavailable`, Android
remains `not_run: device unavailable`, and seven-day UAT remains `not_run`. Task 8
is still blocked until redesign Tasks 2—5 and an independent clean review finish.

## Approved quarantine redesign — Task 1 fix round 1/5

The first independent review found two Important gaps: a present migration marker
could still be followed by `create_all`/`ALTER` repair, and open cleanup identity
was based on raw path text without durable Windows-equivalent identity or strict
UUID/lease relationships.

Observed RED before production changes:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_schema.py -q
25 failed, 5 passed in 2.99s

python -m pytest backend/tests/content/test_artifact_cleanup_schema.py::test_cleanup_read_is_strict_and_has_no_mutation_defaults -q
1 failed in 0.35s
```

Implemented findings:

- Startup reads `task8_artifact_quarantine_v1` before `Base.metadata.create_all`.
  When the marker exists, missing queue/build-token columns, CHECKs, triggers or the
  exact partial index make the database unavailable; no DDL repair is attempted.
- Marker-absent baseline and empty half-migrations remain retry-safe. DDL and startup
  recovery complete before the marker is written; unsafe populated legacy cleanup
  rows fail closed for isolated manual migration.
- Cleanup rows persist `path_key`, derived from an NFC-normalized, Windows-casefolded
  canonical relative path. Absolute paths, backslashes, colon paths, empty/dot/
  whitespace segments, trailing dot/space, controls, reserved device stems and
  non-NFC spellings are rejected by both Pydantic and SQLite CHECKs.
- Open-row uniqueness now uses `(owner_type, owner_id, path_key)` so case-equivalent
  Windows paths cannot create separate live cleanup identities.
- Cleanup IDs, owner IDs and lease tokens must be canonical UUIDs. A `claimed` row
  must have exactly one canonical token and expiry; every non-claimed row must have
  neither. Strict read validation mirrors the physical contract.
- Canonical `content_packages.build_token` is enforced for direct inserts and updates
  by physically validated SQLite triggers, including legacy tables where SQLite
  cannot add a column CHECK in place.
- Historical interrupted package rows with noncanonical owner/path identity retain
  their files and fail startup for manual migration rather than being silently
  normalized or assigned a false cleanup owner.

Final fix-round verification:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_schema.py backend/tests/content/test_content_schema_migration.py -q
34 passed in 2.58s

python -m pytest backend/tests/content -q
126 passed in 12.55s

python -m pytest backend/tests -q
439 passed, 1 skipped in 25.13s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only Windows LF/CRLF notices.
Bailian remains `not_run: BAILIAN_API_KEY unavailable`, Android remains
`not_run: device unavailable`, and seven-day UAT remains `not_run`. Task 8 remains
blocked pending redesign Tasks 2—5 and independent clean review.

## Approved quarantine redesign — Task 2

The cleanup service was implemented test-first. The first focused run was the
expected RED because `backend.app.features.content.cleanup` did not exist:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_service.py -q
ModuleNotFoundError: No module named 'backend.app.features.content.cleanup'
```

Three additional ownership tests were observed RED (`3 failed, 19 passed`): an
original path could reappear during the grace period, an owner UUID could be reused
at another material path, and a second cleanup could reference the same hardlinked
file. A final delete-handle race was independently observed RED (`1 failed`): a
reference inserted after the delete handle opened did not yet cancel deletion.

Implemented in this task:

- Added frozen `ArtifactCleanupCandidate` and `ArtifactCleanupService` with strict,
  idempotent enqueue; ordered SQLite CAS claims; five-minute durable leases;
  expired-lease recovery; one-batch execution; and strict list/get projections.
- Added a fail-closed `trusted` / `ambiguous` / `missing` artifact inspection helper.
  Canonical containment, every parent link/junction, regular-file type, size, SHA-256
  and physical identity are verified without holding a SQLite write transaction.
- Pending files are atomically renamed on the same volume to
  `artifacts-quarantine/{gc_id}/{original_name}`. Lease, reference and file identity
  are checked before and after the move; ambiguous move/commit outcomes retain the
  quarantine bytes and become `needs_human`.
- Quarantined files receive an exact 24-hour grace deadline. Final deletion rechecks
  the original path, owner and all material/package/cleanup references, including
  Windows-equivalent paths and hardlinks. The opened Windows deletion handle must
  still match the expected identity and performs one last database authorization;
  a late reference cancels deletion.
- Missing files are recorded `deleted/already_missing` only after proving there is
  no live reference. Links, junctions, owner mismatch, file replacement, original
  path reappearance, database ambiguity and failed deletion remain visible as
  `needs_human` without deleting bytes.

Final Task-2 verification:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_service.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q
55 passed in 4.69s

python -m pytest backend/tests/content -q
149 passed in 13.61s

python -m pytest backend/tests -q
462 passed, 1 skipped in 33.43s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only Windows LF/CRLF notices.
Bailian remains
`not_run: BAILIAN_API_KEY unavailable`, Android remains
`not_run: device unavailable`, and seven-day UAT remains `not_run`. Task 8 remains
blocked until redesign Tasks 3—5 and an independent clean review finish.

## Approved quarantine redesign — Task 2 fix round 1/5

The independent review's one Critical, five Important and one Minor groups were
captured before the production fixes. The first combined schema/service run was
observed RED:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_schema.py backend/tests/content/test_artifact_cleanup_service.py -q
32 failed, 31 passed
```

The RED cases covered a reference inserted after final-delete authorization,
same-byte replacement with a new physical identity, expired-lease missing paths,
candidate/schema bounds, a grace period incorrectly anchored before the move, and
a target-parent swap during quarantine rename. A commit-fault recovery case and a
select/update expired-lease race were retained as explicit regressions.

Implemented review findings:

- Final deletion now opens and verifies the exact Windows file handle first, then
  enters one short SQLite `BEGIN IMMEDIATE` boundary. Inside that boundary it
  revalidates cleanup ID/state/token/expiry and persisted quarantine identity,
  compares a complete material/package/open-cleanup reference snapshot, performs
  only the identity-bound handle disposition, writes the exact `deleted` CAS and
  commits immediately. A concurrent reference writer is blocked; after release it
  observes `deleted` and does not create a stale reference.
- Hashing, bounded content reads, rename and waits remain outside write
  transactions. If handle deletion succeeds but commit acknowledgement fails, the
  durable row remains recoverable; after lease expiry, the missing artifact is
  finalized only after another no-reference proof.
- Quarantine volume ID, file ID, size and mtime-nanoseconds are persisted and
  physically validated by the model, migration, strict schema and SQLite triggers.
  A same-byte new inode is retained as `needs_human`.
- Same-volume quarantine uses a verified source handle while holding a verified
  non-reparse target-directory handle that denies delete sharing. The Windows
  `FILE_RENAME_INFO` operation uses the locked absolute target and a correctly
  terminated buffer; a parent-swap attempt cannot redirect bytes outside runtime.
- Missing branches first prove an owned, unexpired lease. Every terminal transition
  is an expiry-aware token CAS, and expired-lease recovery uses one conditional
  update matching the originally selected state/token/expiry so a new claim wins.
- Candidate, Pydantic and SQLite contracts share the 1000-character path bound and
  independent 250 MiB hard size cap. Validation occurs before enqueue, and bounded
  processing failures cannot leave a poisoned owned claim.
- The 24-hour grace deadline is calculated from a fresh clock after successful move
  and post-move identity/reference verification. The plan and design now document
  final handle disposition as the sole bounded file-I/O/write-transaction exception.

Final fix-round verification:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_service.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q
63 passed in 5.93s

python -m pytest backend/tests/content -q
159 passed in 15.73s

python -m pytest backend/tests -q
472 passed, 1 skipped in 29.45s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only the repository's Windows
LF/CRLF notices. Bailian remains `not_run: BAILIAN_API_KEY unavailable`, Android
remains `not_run: device unavailable`, and seven-day UAT remains `not_run`. Task 8
remains blocked pending redesign Tasks 3—5 and independent review.

## Approved quarantine redesign — Task 2 fix round 2/5

The three Important review groups were added as focused regressions before the
production changes. The initial RED runs were:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_service.py -q -k "fresh_clock or commit_fault or disarm_failure or deleted_quarantine"
4 failed, 30 deselected

python -m pytest backend/tests/content/test_artifact_cleanup_schema.py -q -k "reference_guard or cannot_reference or quarantined"
3 failed, 1 passed, 33 deselected
```

The failures showed that a long missing-file reference check could finalize with an
expired lease, a commit fault left the armed Windows deletion to complete on handle
close, the disarm path did not exist, and material/package writes could reference a
quarantine path. A separate commit-acknowledgement-loss probe was observed RED
(`1 failed`) before handling the already-committed database outcome.

Implemented review findings:

- Missing-file finalization no longer receives the process-start timestamp.
  `_mark_deleted` reads a fresh clock immediately before its exact terminal CAS and
  requires `lease_expires_at > fresh_now`; crossing expiry during reference checking
  leaves the row claimed and incomplete.
- Windows deletion disposition is now reversible while the verified file handle
  remains open. Final deletion arms the handle, performs the exact database UPDATE
  and commits. Any UPDATE/commit failure first attempts `FileDispositionInfo(False)`.
  A successful disarm retains the exact file and rolls back to the original claimed
  lease; if commit actually succeeded but acknowledgement was lost, the retained
  artifact is persisted as `needs_human/delete_outcome_ambiguous`.
- If disarm fails, the existing `BEGIN IMMEDIATE` boundary remains held while the
  identity-bound cleanup row is changed to
  `needs_human/delete_outcome_ambiguous` and committed. Only then is the handle
  closed. The concurrency regression proves a writer cannot proceed through this
  interval and observes the durable human-review state after release.
- Added the deterministic `windows_artifact_path_key` SQLite UDF plus four exact
  material/package INSERT/path-UPDATE triggers. Claimed, quarantined, deleted and
  needs-human quarantine paths remain reserved under case, NFC, separator, dot and
  Windows trailing-dot/space equivalence.
- Added the retry-safe, physically validated
  `task8_artifact_quarantine_reference_guard_v1` marker. Missing or weak triggers
  fail startup closed without repair. Historical material/package conflicts fail
  migration closed and retain bytes.
- Content material/package flows check the same quarantine reservation before a
  managed path is created, reused or rebuilt and return the explicit state error
  `Artifact path is reserved by quarantine cleanup.` Database triggers remain the
  authoritative boundary for direct SQL and check/use races.

Final fix-round verification:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_service.py backend/tests/content/test_artifact_cleanup_schema.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q
106 passed in 10.23s

python -m pytest backend/tests/content -q
168 passed in 17.81s

python -m pytest backend/tests -q
481 passed, 1 skipped in 33.26s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only the repository's Windows
LF/CRLF notices. Bailian remains `not_run: BAILIAN_API_KEY unavailable`, Android
remains `not_run: device unavailable`, and seven-day UAT remains `not_run`. Task 8
remains blocked pending redesign Tasks 3—5 and independent review.

## Approved quarantine redesign — Task 2 fix round 3/5

The two Important review groups were first reproduced as focused failures:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_schema.py -q -k "cleanup_insert_and_update or missing_reverse"
2 failed, 39 deselected

python -m pytest backend/tests/content/test_artifact_cleanup_service.py -q -k "move_commit_failure or reference_created_during_move"
2 failed, 33 deselected

python -m pytest backend/tests/content/test_artifact_cleanup_service.py -q -k "unproven_moved_fact"
1 failed, 35 deselected
```

Implemented review findings:

- Added physically validated reverse cleanup guards for direct queue INSERT and
  quarantine-path/state UPDATE. A protected cleanup record cannot capture any
  Windows-equivalent material or package path, including case, NFC and trailing
  dot/space variants. The reference-guard marker now requires all six exact
  triggers; missing, weak, or historically conflicting installations fail closed.
- Added an expiry-aware moved-file `needs_human` CAS that persists quarantine path,
  volume/file identity, size, mtime, category and fresh update time while clearing
  the lease. Confirmed moves that fail post-move identity, reference, lease or
  commit checks retain the file with these durable facts.
- Ambiguous commit acknowledgement is verified read-only. If the moved fact cannot
  be proven durable, the record remains claimed rather than reporting success; an
  expired-lease retry recognizes the deterministic quarantine target and records
  the recovered path and identity for service reads after restart.

Final fix-round verification:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_service.py backend/tests/content/test_artifact_cleanup_schema.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q
109 passed in 10.68s

python -m pytest backend/tests/content -q
171 passed in 18.57s

python -m pytest backend/tests -q
484 passed, 1 skipped in 33.66s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only the repository's Windows
LF/CRLF notices. Bailian remains `not_run: BAILIAN_API_KEY unavailable`, Android
remains `not_run: device unavailable`, and seven-day UAT remains `not_run`. Task 8
remains blocked pending redesign Tasks 3—5 and independent review.

## Approved quarantine redesign — Task 2 fix round 4/5

The remaining live-lock was reproduced before the production change. The first
focused run had two expected behavior failures and one test-harness error: a material
could reserve the deterministic future quarantine path while the cleanup was still
pending and leave the moved record claimed; a fabricated
`needs_human/live_reference` moved fact without any matching reference was accepted;
and the startup fixture initially used a raw SQLite connection without the required
deterministic UDF. The fixture was corrected to use the real configured engine, and
the startup validation regression is retained alongside the two direct regressions.

Implemented review finding:

- The reverse cleanup guard now has one narrow moved-file exception. It accepts a
  conflicting quarantine path only when the row becomes `needs_human`, the category
  is exactly `live_reference`, the complete persisted physical identity is present,
  and a material/package reference is Windows-equivalent to the quarantine path.
- Existing live-reference outcomes against the original path remain valid. A moved
  live-reference fact must have a complete identity and a persisted reference to the
  original or quarantine path; fabricated moved facts fail closed.
- A pending cleanup ignores only a reference to its deterministic not-yet-existing
  destination long enough to perform the verified move. Immediately after the move,
  the same case/NFC/trailing-dot-equivalent reference becomes `live_reference` and
  is persisted by one expiry-aware CAS with the lease cleared.
- The material/package forward guards still reserve every protected quarantine
  path. A second equivalent reference is rejected, and `needs_human` records are
  neither claimable nor recoverable as expired leases.
- Startup data validation mirrors the trigger exception: the one explicit moved
  conflict is accepted, while a claimed `live_reference` moved fact without a real
  reference or complete identity makes database startup fail closed.

Final fix-round verification:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_service.py backend/tests/content/test_artifact_cleanup_schema.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q
112 passed in 14.68s

python -m pytest backend/tests/content -q
174 passed in 18.83s

python -m pytest backend/tests -q
487 passed, 1 skipped in 34.58s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only the repository's Windows
LF/CRLF notices. Bailian remains `not_run: BAILIAN_API_KEY unavailable`, Android
remains `not_run: device unavailable`, and seven-day UAT remains `not_run`. Task 8
remains blocked pending redesign Tasks 3—5 and independent review.

## Approved quarantine redesign — Task 2 fix round 5/5

The remaining Important reference-classification gap was reproduced before the
production change. The focused RED evidence was:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_schema.py -q -k "failed_package_owner"
2 failed, 43 deselected

python -m pytest backend/tests/content/test_artifact_cleanup_service.py -q -k "material_owner_at_original"
1 failed, 37 deselected

python -m pytest backend/tests/content/test_artifact_cleanup_service.py -q -k "same_package_owner_nonoriginal"
1 failed, 38 deselected
```

A mutation check also proved the exact failed-package Windows-equivalence
regression fails when classification is changed back to the prior strict
canonical-path comparison.

Implemented review finding:

- Reverse cleanup triggers and startup data validation now consume the same SQL
  reference-predicate builder over typed material/package rows. Classification
  includes reference type, ID, package status, Windows-equivalent path, SHA-256
  and size instead of accepting any row at the original path.
- An exact failed content-package owner at the Windows-equivalent original path
  with the cleanup's expected SHA-256 and size remains the cleanup source; it is
  excluded from live-reference evidence and cannot authorize a fabricated moved
  quarantine path or physical identity.
- A material owner at its Windows-equivalent original path with the expected
  identity remains a live persisted material and stops cleanup before a move. It
  cannot authorize a fabricated moved fact. Reused owner IDs with a different
  path or identity remain `owner_identity_mismatch`.
- A moved `needs_human/live_reference` exception requires an identity-matching
  external Windows-equivalent reference. A content package reusing the same owner
  ID qualifies only at the non-original deterministic quarantine path; a mismatched
  SHA-256 or size fails closed. The legitimate future-quarantine-path race remains
  durable under case, NFC and Windows trailing-dot equivalence.
- Python service classification now mirrors these ownership and identity rules,
  including Windows path equivalence for the exact failed package source.

Final fix-round verification:

```text
python -m pytest backend/tests/content/test_artifact_cleanup_service.py backend/tests/content/test_artifact_cleanup_schema.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q
119 passed in 12.20s

python -m pytest backend/tests/content -q
181 passed in 20.33s

python -m pytest backend/tests -q
494 passed, 1 skipped in 35.86s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0; Git emitted only the repository's Windows
LF/CRLF notices. Bailian remains `not_run: BAILIAN_API_KEY unavailable`, Android
remains `not_run: device unavailable`, and seven-day UAT remains `not_run`.
Task 8 remains blocked pending redesign Tasks 3—5 and independent review.

## Approved quarantine redesign — Task 8R-5 final closeout implementation

The two approved Task 8 minors and the matching regenerate boundary were first
reproduced against the real export and workflow code:

```text
python -m pytest backend/tests/content/test_export.py backend/tests/content/test_workflow.py -q
4 failed, 15 passed
```

The failures proved that ZIP container overhead could cross the archive limit,
the uncompressed aggregate did not have an independent cap, an unconfigured model
masked a missing product as HTTP 503, and a valid rejected item acquired a false
`regenerate` review before model readiness failed.

Implemented closeout:

- `MAX_PACKAGE_BYTES` now bounds the final serialized archive bytes. A separate
  `MAX_UNCOMPRESSED_PACKAGE_BYTES` bounds the entry plus manifest aggregate, while
  the compression-ratio defense remains independent.
- Boundary tests reject archive overhead beyond the cap and independently reject
  oversized uncompressed aggregates. A valid archive close to the byte cap remains
  readable and projects `availability=available` rather than `corrupt`.
- The create API no longer checks model configuration ahead of the content service.
  Missing products and other persisted business errors are therefore returned first;
  a fully valid request with no configured provider still returns truthful HTTP 503.
- Regeneration validates item trust, revision and rejected state, reconstructs and
  validates the persisted product/opportunity/evidence/material context, then checks
  model readiness before reserving the item or writing a `regenerate` review. An
  unconfigured adapter is never called.

Final implementation verification before independent review:

```text
python -m pytest backend/tests/content/test_export.py backend/tests/content/test_workflow.py -q
19 passed in 3.12s

python -m pytest backend/tests/content -q
267 passed in 41.96s

python -m pytest backend/tests -q
580 passed, 1 skipped in 62.53s

python -m compileall -q backend/app backend/tests
git diff --check
```

Compilation and diff checks exited 0. The only test warnings were the existing 32
Python 3.12 sqlite datetime-adapter deprecation warnings. A source scan found no
physical-delete helper, `unlink`, `os.remove`, `rmtree` or `DeleteFile` call in the
content request API/service, database startup or app startup paths. Cleanup routes
remain GET-only. Export manifest data still states `automatic_publish=false`; no
demo data, fake progress or automatic-publish completion claim was introduced.

Redesign implementation/review-fix ranges are 8R-1 `54a7ee1..8db03a4`, 8R-2
`ae7b03f..6256dc8` plus user-approved stabilization `6c4bf9f..0edf06a`, 8R-3
`606a857..c682d1b`, and 8R-4 `f3094b0..d2c00b2`. These ranges contain the
review-driven fixes recorded in the ledger. Task 8 is not yet marked complete: the
final cross-boundary independent review must still report no Critical or Important
findings.

Live Bailian remains `not_run: BAILIAN_API_KEY unavailable`; Android remains
`not_run: device unavailable`; seven-day UAT remains `not_run`. None is inferred
from the automated suite.
