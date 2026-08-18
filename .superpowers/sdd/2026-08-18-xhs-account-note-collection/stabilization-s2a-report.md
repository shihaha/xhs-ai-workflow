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
