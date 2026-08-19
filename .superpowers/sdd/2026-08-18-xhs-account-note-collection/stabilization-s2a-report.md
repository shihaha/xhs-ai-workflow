# Stabilization S2A report — fix round 1/5

Date: 2026-08-19

## Scope and outcome

This round closes the follow-up C1/C2/I1/I2 findings at the account/note
collection boundary. It does not change S2B append-only/versioned persistence or
analysis TOCTOU behavior, Bailian behavior, or the untracked `research/` tree.

The boundary is now based on the real pinned source at
`xhs-cli@3ce71415dc0816ebb4c3f547baf6c08fb3d5cb5a`. Its list outputs,
`userPageData`/`userInfo`, nested `noteCard`/stats and `xsecToken` shapes remain
covered by the adapter tests and controlled live fake. The fake no longer
invents a top-level dictionary contract.

## C1 — trusted state and a read-only pinned wrapper

- Production launches only `[python, -I, <absolute repository wrapper>,
  <allowlisted command...>]`; it never invokes an operator-selected XHS command,
  a shell, or request-supplied argv.
- The wrapper checks package version `0.1.4` and normalized source SHA-256 for
  the four audited modules before importing/dispatching them. The clean pinned
  checkout reproduced all four embedded digests.
- The only credential input is an externally prepared, bounded
  `.xhs-cli/cookies.json`. Arbitrary valid cookie names are retained and `a1`
  plus `web_session` are required. The parent reads it through a pinned handle,
  canonicalizes it, and sends it to the child over stdin. Credential values do
  not enter argv, environment, logs, durable artifacts, or failure facts.
- On Windows, every runtime/state/config path component is opened with
  `FILE_FLAG_OPEN_REPARSE_POINT`, checked for the expected type and final path,
  and held for the complete command. The cookie file must have exactly one hard
  link; volume/file identity, size and link count must match before and after the
  handle read. Any reparse, hard link, identity change or validation failure is
  fail-closed. Private runtime directories are created one component at a time
  only after their parents are handle-pinned, so a hostile reparse cannot divert
  a write outside the runtime.
- The child receives an allowlisted environment whose profile, AppData, temp and
  XDG locations all point at the private runtime. The wrapper replaces the
  pinned CLI's saved-cookie/browser-cookie, QR/login and clear/save-cookie hooks;
  it also disables token-cache load/write and note xsec-cache writes before
  dispatch. Missing prepared state returns `needs_human/login_required`; unsafe
  prepared state returns `needs_human/external_state_untrusted`. It never falls
  back to a normal browser/profile.
- Recursive redaction continues to cover case-insensitive `Name`/`Value`
  credential records, whole `cookies`/`tokens` containers, arbitrary cookie
  names, `xsecToken` and `xsec_token` before result/artifact persistence.

## C2 — bounded whole-process-tree ownership

- Windows uses `CreateProcessW` with an inherited-handle allowlist and
  `PROC_THREAD_ATTRIBUTE_JOB_LIST`, so the direct child enters a private Job
  Object atomically at creation. `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` provides a
  final containment backstop for every descendant, including a browser spawned
  by the pinned CLI.
- Stdout and stderr are drained concurrently with independent in-flight byte
  caps while stdin is written on its own thread. One deadline covers process
  setup, execution, descendant/pipe completion and cleanup.
- Timeout, either-stream overflow and cooperative cancellation terminate the
  whole Job Object, wait for zero active processes, finish pipe threads, reap the
  direct child and close process/job/pipe/CRT handles. A direct child exit is not
  success while descendants remain or retain a pipe.
- Regression coverage includes descendants with and without inherited pipes,
  a delayed descendant marker, cancellation latency, repeated normal completion
  handle counts, and a controlled mid-setup fd-conversion failure. The latter was
  found during final resource review and fixed so partial ownership transfer
  cannot leak a converted pipe handle.
- The POSIX fallback uses an isolated process group with the same deadline,
  output and cancellation categories; the production host-specific guarantee
  was exercised on Windows.

## I1 — shutdown cancellation and late-fact fence

- `XhsCollectionService.close()` fences new admissions, propagates one cancel
  event through service → adapter → bounded runner, cancels jobs/futures and
  joins only within a single 0.25-second budget.
- The adapter tracks active commands and reports unsafe close rather than
  pretending success. FastAPI lifespan completes the other shutdown cleanup and
  raises a sanitized error if XHS close was unsafe.
- Both a cooperative runner test and a real adapter/Job-Object descendant test
  prove bounded close, descendant-marker suppression and no late database facts
  or success artifact after shutdown.

## I2 — linear evidence and hard result/artifact caps

- Each source response appears once in `CollectionResult.raw_evidence`, keyed by
  source and paired with a SHA-256 response reference. Per-item and rejected-item
  evidence contains only the reference, zero-based row index and that row (or a
  bounded profile row); it does not repeat the whole payload.
- Public expected counts remain bounded to 1,000 notes (account collection adds
  its single profile item internally). A 1,000-row regression proves one shared
  response and sub-megabyte, linear serialized output.
- Adapter-transformed results and service artifacts are incrementally measured
  against configured hard byte caps. An oversized transformed result fails
  before success can be reported; an oversized service artifact writes only a
  bounded failure artifact and commits no success fact/artifact.

## TDD evidence

The review requirements were first encoded as a new hardening suite. Its first
run was RED:

```text
12 failed
```

The same initial cases became GREEN after the mechanism changes. Additional
reparse, real service-to-runner cancellation, normal-handle and resource-setup
cases expanded the final suite. The setup-failure resource test was separately
observed RED (`1 failed`) before its ownership fix. Final result:

```text
17 passed in 5.26s
```

## Final controlled verification

- Focused XHS, Settings, health and guarded-live suites:
  `374 passed, 1 skipped`.
- Guarded live contract alone: `2 passed, 1 skipped`; the skip explicitly says
  `not_run: XHS_LIVE_TEST=1 was not supplied`.
- Analysis regression: `90 passed, 1 skipped`.
- Controlled frontend E2E repeat gate: `5 passed` with `--repeat-each=5`.
- `scripts/verify.ps1`: exit 0; full backend `1050 passed, 2 skipped`; Python
  compile passed; frontend `47 passed`; production build passed; controlled E2E
  `1 passed`; npm audit found `0 vulnerabilities`; tracked-secret and release
  boundary scans passed.
- Independent `git diff --check`, compile/release-boundary checks and unsafe
  process scans passed. No production `subprocess.run`, `capture_output=True` or
  `shell=True` exists in the new boundary. The only browser/login/cache hook
  matches are the wrapper assignments that replace them with disabled/no-op
  implementations.
- Staged-scope audit contains no S2B analysis/persistence implementation, no
  Bailian implementation and no `research/` file.

## Live status

Real authenticated XHS execution remains exactly:

```text
not_run
```

`XHS_LIVE_TEST=1` and external authenticated state were not supplied, and the
host interpreter has no usable live `xhs_cli` installation for this gate. No
real account/search success, identity or fact is claimed. Controlled fake/E2E
evidence is software verification only. S2B remains pending.

# Stabilization S2A report — fix round 2/5

Date: 2026-08-19

## Scope and outcome

This follow-up closes the four round-2 findings without changing S2B
append-only/versioned persistence or analysis TOCTOU behavior, Bailian behavior,
or the untracked `research/` tree. The authenticated live gate remains separate
and was not inferred from controlled tests.

## C1 — verified bytes are the executed XHS package

- The pinned manifest now covers every executable Python source in the audited
  `xhs_cli` package: `__init__.py`, `exceptions.py`, `auth.py`, `client.py` and
  `cli.py`. The wrapper rejects a missing, extra or changed package source.
- The wrapper locates one filesystem package without using Python import
  loaders, reads and hashes every source once, retains those exact bytes, and
  compiles them with `verified-memory:` origins. It pre-registers only the exact
  controlled `ModuleType` set, executes it in dependency order and rejects any
  pre-existing or additional `xhs_cli` module.
- No verified source path is reopened after hashing, and neither ordinary
  import resolution nor `.pyc` loading can select package code. Process-level
  tests trap alternative loaders and malicious bytecode, mutate all source
  files after the verified reads, and independently tamper each of the five
  pinned files. Only retained verified bytes execute.

## C2 — post-CreateProcess cleanup is one bounded state machine

- Capture construction and thread start are separate. Every successfully or
  partially started reader is recorded, cancellable and joined only against the
  shared execution deadline.
- After any exception following `CreateProcessW`, cleanup first terminates and
  closes the kill-on-close Job Object, then cancels synchronous reader I/O,
  reaps the direct process and closes owned pipe/process handles. Timeout,
  overflow and cancellation use the same ordering.
- Parent pipes now use a minimal unbuffered raw-fd owner, so cleanup does not
  enter a potentially blocking high-level `stream.close()`. Sanitized exception
  translation is raised outside the caught low-level exception context so
  traceback retention cannot accumulate reader-thread handles.
- Regressions cover a partial reader start, a later pipe conversion failure,
  a descendant that retains a pipe/tries to write a delayed marker, a failed
  `TerminateJobObject` call with kill-on-close backstop, a deliberately blocking
  high-level close, and repeated failures with stable process-handle counts.

## I1 — staged evidence and truthful commit acknowledgement

- Result and failure payloads are first written to unique, fsynced `.stage`
  files. A formal JSON path is linked only after the job is re-read as running,
  the shutdown fence is still open, the final-state CAS wins and all normalized
  facts are inside the same transaction.
- Commit-acknowledgement loss is resolved by directly re-reading the job state,
  exact single artifact row, bounded file size and SHA-256. A committed result
  remains successful; a pre-commit rollback cannot masquerade as success.
- Shutdown, CAS loss, transaction rollback and promotion-acknowledgement loss
  move any formal link back into private staging and remove only the verified
  staging entry. A side-effecting `os.link` failure is detected with same-file
  identity before rollback.
- Permanent deletion is confined to the reviewed XHS staging cleanup module.
  It accepts only the fixed `evidence/xhs/.staging/<validated>.stage` shape,
  uses the existing handle-bound Windows deletion primitive (or parent `dir_fd`
  containment on POSIX), and refuses formal `.json` evidence paths. The release
  boundary scanner explicitly recognizes only that module.
- Tests prove no late account facts, success/failure formal orphan or staging
  file remains after shutdown fencing, CAS loss, result rollback, failure
  rollback or uncertain promotion. Successful commit leaves exactly one formal
  artifact consistent with the database.

## I2 — parent handles precede every credential probe

- Runtime is opened once, then every state component, `.xhs-cli` and
  `cookies.json` is opened relative to the already-held parent handle on
  Windows (`NtCreateFile`) or parent `dir_fd` on POSIX. No full cookie path is
  probed before those parents are fixed.
- Missing prepared state remains bounded `needs_human/login_required`; a state
  component reparse fails closed as `external_state_untrusted` without touching
  the external cookie target or creating a private runtime there.
- Private runtime creation is likewise handle-relative. The state, config,
  cookie and private-directory handles remain held for the child lifetime.

## TDD evidence

The required defects were observed before their fixes:

- source binding: `7 failed` (missing `exceptions.py` coverage and loader-bound
  execution);
- parent-first isolation: `1 failed` (the full cookie path reached an external
  reparse target);
- post-create cleanup: `2 failed` (raw setup exceptions/tree survival), plus a
  separate blocking-close RED (`1 failed`);
- finalization: shutdown/CAS/rollback cases failed before staging, and the
  side-effecting promotion acknowledgement test separately failed with one
  formal orphan.

The dedicated round-2 hardening suite is now `17 passed`. The combined
round-2/round-1/service regression is green.

## Final controlled verification

- Focused XHS, Settings, health and guarded-live suites:
  `395 passed, 1 skipped`.
- Analysis regression: `90 passed, 1 skipped`.
- Controlled frontend E2E repeat gate: `5 passed` with `--repeat-each=5`.
- `scripts/verify.ps1`: exit 0; full backend `1072 passed, 2 skipped`; Python
  compile passed; frontend `47 passed`; production build passed; controlled E2E
  `1 passed`; npm audit found `0 vulnerabilities`; tracked-secret and release
  boundary scans passed.
- Independent `git diff --check`, compile, fixed-argv/no-shell/process-boundary,
  source-loader and staged-scope scans passed. No S2B/Bailian implementation or
  `research/` file is part of this round.

## Live status

Real authenticated XHS execution remains exactly:

```text
not_run
```

`XHS_LIVE_TEST=1` and external authenticated state were not supplied. No real
account/search result, identity or persisted fact is claimed.

# Stabilization S2A report — fix round 3/5

Date: 2026-08-19

## Scope and decisions

This round closes the review finding about commit uncertainty and crash
recovery without changing S2B evidence semantics, Bailian integration, or the
untracked `research/` tree.

- Every XHS artifact attempt now has one durable
  `xhs_artifact_promotion_journal` row binding the job, artifact kind,
  producer, exact staging/final names, digest, size, physical file identity,
  target job state, and recovery state. The physical schema, checks, indexes,
  foreign keys, and binding/no-delete triggers are independently validated by
  migration marker `xhs_artifact_promotion_journal_v1`. A present marker is
  validation-only; malformed populated or half-migrated state fails closed.
- The normal state machine is `prepared -> promoted -> completed`. Prepared
  identity is committed before promotion. After promotion, the artifact row,
  normalized account-note facts, terminal job transition, and journal
  `completed/committed` transition are committed in one database transaction.
- Commit acknowledgement is classified as `committed`, `rolled_back`, or
  `unknown`. An unknown result never removes or moves formal evidence. It is
  left journal-owned for bounded startup reconciliation.
- Startup reconciliation enumerates journal rows only. It confirms exact
  committed files, restores a committed exact stage when the formal file is
  missing, safely demotes/discards an exact uncommitted identity, and marks
  contradictions or replacements `needs_human` without touching the replaced
  file. Arbitrary/unjournaled JSON is never scanned or deleted.
- `TrustedXhsArtifactStore` pins the private runtime evidence parents. Windows
  creates and opens descendants relative to held directory handles and uses
  handle-relative `NtSetInformationFile` rename; POSIX uses `dir_fd`,
  `O_NOFOLLOW`, and relative rename/unlink. Creation, bounded writes, fsync,
  reads, promotion, recovery, demotion, and staging cleanup all revalidate the
  same single-link regular-file identity. Reparse points, added hardlinks,
  name swaps, oversized files, and ambiguous identities fail closed.

## TDD evidence

The findings were reproduced before implementation:

- fresh journal schema/marker: `1 failed` while the table did not exist;
- handle-bound artifact store: `2 failed` before the trusted store existed;
- Windows handle-relative promotion: RED with `WinError 87` before switching
  from the incompatible rename API to `NtSetInformationFile`;
- promotion crash recovery: `1 failed` before the persistent state machine;
- physical binding/no-delete triggers: `2 failed` before trigger enforcement;
- restart restore and exact-identity recovery: `2 failed` before writable
  handle-bound recovery was completed.

The final dedicated round-3 hardening suite is `18 passed`. It covers marker
validation-only behavior, half migrations, physical trigger mutation, reparse,
hardlink and identity-swap rejection, promotion crash, prepared and final
commit acknowledgement loss, commit-not-landed rollback, restart restore,
replacement preservation, repeat/concurrent reconciliation, and survival of
unjournaled JSON.

## Final controlled verification

- Focused XHS, Settings, and health suites: `413 passed, 1 skipped`.
- Analysis regression: `90 passed, 1 skipped`.
- Guarded live contract: `2 passed, 1 skipped`; the skip is exactly
  `not_run: XHS_LIVE_TEST=1 was not supplied`.
- Controlled frontend E2E repeat gate: `5 passed` with `--repeat-each=5`.
- `scripts/verify.ps1`: exit 0; full backend `1090 passed, 2 skipped`; Python
  compile passed; frontend `47 passed`; production build passed; controlled
  E2E `1 passed`; npm audit found `0 vulnerabilities`; tracked-secret and
  release-boundary scans passed.
- Independent diff, compile, journal-mutation, fixed-scope, and staged-file
  audits passed. No S2B/Bailian implementation or `research/` file is included.

## Live status

Real authenticated XHS execution remains exactly:

```text
not_run
```

No external authenticated state was supplied and `XHS_LIVE_TEST=1` was not
enabled. This round makes no claim about a real account, search result, identity,
or live persisted fact.

# Stabilization S2A report — fix round 4/5

Date: 2026-08-19

## Scope and decisions

This round closes the five remaining account-note S2A trust and recovery
findings without changing S2B evidence semantics, Bailian integration, or the
untracked `research/` tree.

### C1 — one formal-read provenance gate

- Profile, account-note, and search-result reads now share one trust resolver.
  Formal facts are returned only for an exact succeeded job with exactly one
  completed/committed journal row and one bound artifact row.
- The resolver verifies job type/input, artifact kind/producer/path, strictly
  typed metadata, account or search binding, journal identity, final-file
  identity, size, SHA-256, and payload job binding. Replaced or contradictory
  files fail closed.
- `inconsistent` and `needs_human` evidence remains available to operators in
  the database, but normal account/search reads do not expose it as fact.

### I1 — bidirectional physical binding

- The v2 schema installs and validates exactly seven journal-family triggers:
  journal insert/update/no-delete, parent-job update/delete, and parent-artifact
  update/delete. Parent updates cover every predicate field used by the trust
  contract, including identifiers, type/state/input, job linkage, kind,
  producer, path, and metadata.
- Metadata trigger predicates and startup scans require JSON objects with
  present, correctly typed, NULL-safe-equal `artifact_id`, `job_id`, `sha256`,
  and `size_bytes` values. A textified integer, JSON null, missing key, wrong
  account/search binding, or malformed JSON fails closed.
- Migration certification compares exact table, index, foreign-key, check, and
  trigger definitions and scans populated rows. Runtime parent tampering is
  rejected by SQLite and bypassed historical tampering is rejected on restart.

### I2 — durable allocation intent before staging

- Journal v2 adds `allocating` before `prepared`: the allocating row, owner,
  DB-clock lease, intended names, digest, size, and target state are committed
  before the store or stage file is created. Every newly created stage therefore
  has a durable journal owner.
- After creation, the exact open-handle identity is bound with a CAS transition
  to `prepared`. A stage-creation failure deletes only through the still-held
  exact handle when the platform can prove it; cleanup ambiguity is persisted
  as `needs_human` and the evidence is retained.
- A prepared-commit acknowledgement failure leaves an observable durable
  allocating/prepared row and never performs pathname cleanup. Startup retains
  an old unjournaled stage for manual review instead of sweeping it.
- Populated v1 databases migrate mechanically to v2, retain the v1 history
  marker, receive the v2 marker only after validation, and preserve journal
  rows and evidence.

### I3 — cross-process recovery claims and leases

- `owner_token` and `recovery_lease_expires_at` form a checked pair. Claims and
  renewals are database CAS operations using SQLite's clock, not a process
  clock. Active finalizers hold and refresh their lease through promotion,
  commit, rollback, and acknowledgement handling.
- Worker restart recovery skips a job with an unexpired active finalizer.
  Startup reconciliation may make destructive decisions only after claiming an
  unowned or expired row. Expired work can be reclaimed, while two independent
  Python processes racing for the same row produce exactly one claim winner.
- Allocating ambiguity and identity contradictions are retained and escalated;
  reconciliation does not roll back a live finalizer or delete an unowned file.

### I4 — pathname-race closure

- POSIX promotion uses `renameat2(..., RENAME_NOREPLACE)` with pinned directory
  file descriptors. If the kernel cannot provide that primitive, promotion
  fails closed. No ordinary overwrite-capable rename is used.
- POSIX cleanup retains the stage when deletion-time identity cannot be proved;
  it never performs check-then-name-unlink. Windows continues to promote and
  delete through held handles, including no-overwrite collision behavior.
- The XHS staging implementation contains no ordinary `os.rename` or
  `os.unlink` call.

## TDD evidence

The required defects were observed before implementation:

- formal-read provenance: `3 failed, 1 passed` before the shared trust gate;
- strict/bidirectional physical binding: `4 failed, 5 passed` before the parent
  guards and strict JSON predicates;
- durable intent and recovery ownership: `7 failed, 9 passed` before allocating
  intent, leases, CAS claims, and active-finalizer protection;
- partial-NULL identity was separately RED because the old check admitted it;
- the POSIX forbidden-call scan was RED while a pathname `os.unlink` remained.

The final dedicated round-4 hardening suite is `22 passed`, repeated three
times (`22 passed` each run). It covers formal-read tamper, all seven physical
guards, exact schema/data validation, populated v1-to-v2 migration, allocation
and acknowledgement failures, lease constraints, DB-clock and cross-process
CAS races, expired recovery, retained unjournaled stages, POSIX fail-closed
behavior, and Windows handle/no-overwrite regressions.

## Final controlled verification

- Focused XHS, Settings, and health suites: `435 passed, 1 skipped`.
- Schema migration/hardening suite: `63 passed`.
- Analysis regression: `90 passed, 1 skipped`.
- Guarded live contract: `2 passed, 1 skipped`; the skip is exactly
  `not_run: XHS_LIVE_TEST=1 was not supplied`.
- Controlled frontend E2E repeat gate: `5 passed` with `--repeat-each=5`.
- `scripts/verify.ps1`: exit 0; full backend `1112 passed, 2 skipped`; Python
  compile passed; frontend `47 passed`; production build passed; controlled
  fresh-runtime E2E `1 passed`; npm audit found `0 vulnerabilities`;
  tracked-secret and release-boundary scans passed.
- Independent diff, staged-scope, trigger-schema, strict-metadata,
  journal-mutation, forbidden-POSIX-call, and secret scans passed. No
  S2B/Bailian implementation or `research/` file is included.

## Live status

Real authenticated XHS execution remains exactly:

```text
not_run
```

No external authenticated state was supplied and `XHS_LIVE_TEST=1` was not
enabled. This round makes no claim about a real account, search result, identity,
or live persisted fact.

# Stabilization S2A report — fix round 5/5

Date: 2026-08-19

## Scope and decisions

This final S2A round closes the two remaining review findings: normalized
account facts were not yet content-bound to their verified artifact, and the
initial allocating-journal commit acknowledgement was not yet covered by the
same three-state recovery contract. S2B analysis semantics, Bailian, and the
untracked `research/` tree remain outside this commit.

### C1 — exact normalized-fact content binding and physical freeze

- Account persistence, runtime reads, and restart certification now share one
  strict normalization function. It derives every persisted profile and note
  field from the exact verified artifact: identifiers, owners, source URLs,
  text, public counters, raw evidence, raw digest, collection job/artifact,
  timestamp, note order, and note count.
- A runtime account read holds one database snapshot, reopens the exact
  journal-owned artifact through its recorded physical identity, validates its
  digest and envelope, normalizes it again, and compares the complete profile
  and ordered note rows. Any changed field, owner, order, count, or raw evidence
  fails closed.
- Migration `xhs_account_fact_content_binding_v4` validates populated snapshots
  against readable formal artifacts before certification, validates its exact
  trigger definitions on every marked restart, and rejects historical content
  tampering. Missing or recovery-invalidated evidence remains unavailable to
  normal reads and is left to the existing artifact recovery state machine.
- Two physical update triggers freeze all fields of formally metadata-bound
  profile and note rows. A legitimate recollection replaces the complete
  snapshot by delete-and-insert; it never mutates a previously verified row.

### I1 — initial allocation acknowledgement and no-stage recovery

- The first allocating-journal insert is represented by one exact allocation
  binding. If its commit raises, a bounded fresh read classifies the result as
  `committed`, `rolled_back`, or `unknown`. Only an exact committed row proceeds
  to staging; all other outcomes stop at the service boundary as
  `ArtifactCommitUnknown`, without leaking SQLAlchemy exceptions or creating a
  second journal.
- A durable rolled-back/unknown allocation is converged with a guarded update;
  another allocator's journal is never overwritten. Concurrent initial
  allocators therefore leave one durable owner and one journal.
- Startup can reclaim an `allocating` row whose job is no longer queued/running,
  even if the abandoned lease has not expired. An absent stage closes the row as
  `completed/rolled_back` and leaves the job explicitly `needs_human`; a present
  or ambiguous stage is retained and escalated.
- Worker restart protection now covers only `prepared` and `promoted` physical
  finalizers. This `jobs.py` change is required so an allocating/no-stage job is
  not kept running forever. If recovery wins just before the original creator
  writes its stage, the original process recognizes the exact rolled-back row
  and removes the late stage through its still-held identity-safe handle.

## TDD and integration evidence

The rescued handoff already had `31 passed`, so it was not treated as proof that
the review findings were fully closed. A semantic audit added regressions that
were observed RED before the final fixes:

- six physical-freeze cases for profile/note collection job, artifact, and
  timestamp fields, plus one recovery-before-stage race:
  `7 failed, 16 passed, 31 deselected`;
- after the fixes, the same targeted selection was `23 passed, 31 deselected`;
- the final dedicated round-5 suite is `59 passed`, repeated three fresh times
  (`59 passed` each run).

The first repository verification then exposed three older analysis tests that
attempted direct post-freeze note mutation (`1168 passed, 2 skipped, 3 failed`).
Those fixtures now explicitly model corruption predating the freeze by
temporarily removing and exactly restoring the production trigger. The
production analysis implementation was not changed. The affected selection is
`3 passed, 24 deselected`, and the complete analysis suite is
`90 passed, 1 skipped`.

## Final controlled verification

- Focused XHS, Settings, and health suites: `494 passed, 1 skipped`.
- Dedicated round-5 suite: `59 passed`; three consecutive repeat runs also
  produced `59 passed` each.
- Analysis regression: `90 passed, 1 skipped`.
- Guarded live contract: `2 passed, 1 skipped`; the skip remains exactly the
  opt-in live gate and no external authenticated state was supplied.
- `scripts/verify.ps1`: exit 0; full backend `1171 passed, 2 skipped`; Python
  compile passed; frontend `47 passed`; production build passed; controlled
  fresh-runtime E2E `1 passed`; npm audit found `0 vulnerabilities`;
  tracked-secret and release-boundary scans passed.
- Independent `git diff --check`, trigger, journal, scope, and staged-file scans
  passed. No S2B/Bailian implementation or `research/` file is included.

## Live status

Real authenticated XHS execution remains exactly:

```text
not_run
```

`XHS_LIVE_TEST=1`, the target inputs, and trusted external authenticated state
were not supplied. This round makes no real-account or live-persisted-fact claim.

# Stabilization S2A report — Type Stabilization

Date: 2026-08-19

## Scope and defect

This user-approved S2A-only revision closes one Important type-stability defect
in normalized public counters. Python mapping equality treated JSON integers,
floats, and booleans as equal in cases such as `10 == 10.0` and `True == 1`.
Consequently, a database counter object could differ from the verified artifact
at the JSON type level yet pass the previous complete-snapshot comparison.
Runtime reads could then return that drifted value through the API, and a marked
restart could accept the same drift as certified content.

The audit covers every public counter currently admitted by the S2A artifact
schema, not one special case:

- profile: `followers_count`, `following_count`, `liked_count`, `fans`, and
  `follows`;
- note: `liked_count`, `collect_count`, `comment_count`, `likedCount`,
  `collectCount`, and `commentCount`.

S2B analysis semantics, Bailian, and the untracked `research/` tree remain
outside this revision.

## Type-stable contract

- One shared public-counter validator/comparator is used by both the runtime
  formal-read gate and the marker-present migration/restart certification path.
- A counter object must be a non-null JSON object. Every present key must be in
  the exact profile or note allowlist, and every value must be a non-negative
  Python `int` whose exact type is not `bool`. Nested objects, arrays, strings,
  floats, booleans, and explicit `null` values are rejected.
- Optional source counters remain represented by absence. Canonical sorted JSON
  bytes preserve the admitted JSON type and require the persisted and
  artifact-derived field sets and values to match exactly.
- Runtime database type drift fails closed as `CollectionFactNotFound`; the
  normal profile and note APIs return `404` and never serialize the drifted
  counter payload. The same drift makes marked startup fail with a content-
  binding `SchemaMigrationError`.

## TDD evidence

Before implementation, the new profile/note, runtime/restart, and HTTP API
selection produced `46 failed, 9 passed, 63 deselected`. The 46 failures are the
float/bool equality defects across all eleven fields; the nine already-passing
controls show that older whole-object equality rejected nested, null, missing,
and extra shapes and admitted exact integers.

After the shared type-stable comparison was installed, the identical selection
produced `55 passed, 63 deselected`. The complete affected round-5 and collection
API files then produced `118 passed`.

## Final controlled verification

- Focused XHS, Settings, and health suites: `549 passed, 1 skipped`.
- Dedicated round-5 suite: `112 passed`; three consecutive fresh-process repeat
  runs also produced `112 passed` each.
- Analysis fact-freeze regression: `90 passed, 1 skipped`; production analysis
  code was not changed.
- Guarded live contract: `2 passed, 1 skipped`; the skip is exactly
  `not_run: XHS_LIVE_TEST=1 was not supplied`.
- `scripts/verify.ps1`: exit 0; full backend `1226 passed, 2 skipped`; Python
  compile passed; frontend `47 passed`; production build passed; controlled
  fresh-runtime E2E `1 passed`; npm audit found `0 vulnerabilities`;
  tracked-secret and release-boundary scans passed.
- Independent diff, staged-scope, and counter-field scans passed. No S2B,
  Bailian, analysis implementation, or `research/` file is included.

## Live status

Real authenticated XHS execution remains exactly:

```text
not_run
```

No external authenticated state or target inputs were supplied, and
`XHS_LIVE_TEST=1` was not enabled. This revision makes no real-account or
live-persisted-fact claim.
