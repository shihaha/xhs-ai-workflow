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
