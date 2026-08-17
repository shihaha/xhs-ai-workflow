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
