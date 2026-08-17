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
