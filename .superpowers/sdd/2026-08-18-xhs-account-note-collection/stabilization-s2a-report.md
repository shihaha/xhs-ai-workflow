# Stabilization S2A report

Date: 2026-08-19

## Scope and result

This change closes whole-plan findings C1, C2 and I1 for the account/note
collection boundary only. It does not change append-only/versioned note
persistence, analysis trust revalidation, Bailian behavior, or the untracked
`research/` tree; those remain outside S2A.

The production contract was checked against the clean pinned source tree at
`xhs-cli@3ce71415dc0816ebb4c3f547baf6c08fb3d5cb5a`. The relevant pinned
behaviors are:

- `search --json` and `user-posts --json` emit top-level lists;
- `user --json` returns `userPageData`/`userInfo`, with the public target profile
  under `userPageData.basicInfo` and public counts under `interactions`;
- note rows carry an outer identity/xsec field and public content under nested
  `noteCard`;
- ordinary read commands call the upstream saved-cookie loader and search may
  write `.xhs-cli/token_cache.json`.

S2A now normalizes those shapes, preserves the existing provider-neutral
contract, and fails closed when the external state or response cannot be
trusted.

## Security and process decisions

1. `Settings` owns `XHS_CLI_EXECUTABLE`, `XHS_CLI_STATE_DIR`, timeout and output
   bounds. The state directory must resolve inside `runtime_dir`; none of these
   values or any credential is accepted from the HTTP collection request.
2. Each CLI command revalidates a bounded externally prepared
   `.xhs-cli/cookies.json` containing non-empty `a1` and `web_session` entries.
   If it is missing or malformed, the adapter returns
   `needs_human/login_required` without starting a child.
3. The child receives a new allowlisted environment. `cwd`, `HOME`,
   `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, temporary and XDG paths all point
   into the configured state tree. Consequently an upstream fallback race
   cannot reach the operator's normal browser profile. The upstream xsec cache
   write is allowed only in this isolated state tree; no normal profile/cache
   path is inherited.
4. Every command is a fixed argv list with `shell=False`. No public arbitrary
   argv runner was added.
5. `subprocess.Popen` drains stdout and stderr concurrently with independent
   in-flight limits. Overflow or timeout terminates, escalates to kill when
   needed, reaps the child, closes both pipes and returns only a sanitized
   category. Raw stdout/stderr never becomes a durable failure fact.
6. Credential redaction now treats semantic `Name`/`Value` keys
   case-insensitively, redacts whole `cookies`/`tokens` containers regardless of
   their inner names, and covers both `xsecToken` and `xsec_token`. Ordinary
   public values remain intact.

## TDD evidence

Initial focused RED for the C1/C2/I1 production behaviors:

```text
9 failed, 133 deselected
```

The failures covered pinned list/profile/note shapes, missing/isolated state,
actual credential shapes, stdout overflow, stderr overflow, timeout cleanup and
the Settings state boundary. After implementation, the identical selection was:

```text
9 passed, 133 deselected
```

The guarded live contract then went RED because no production identity probe
existed:

```text
2 failed, 1 skipped
```

After adding fixed `status` text plus `whoami --json` identity validation:

```text
2 passed, 1 skipped
```

The explicit non-duplicated `XHS_CLI_*` environment-name test also went from
one failure to all four Settings tests passing.

## Verification evidence

- Focused XHS, Settings, health and guarded-live suites: `356 passed, 1 skipped`.
- Adapter and Settings suites: `142 passed`.
- Collection service/API/health focused suites: `30 passed`.
- Analysis regression: `90 passed, 1 skipped`.
- Frontend unit suite: `47 passed`.
- Frontend production build: passed.
- Controlled fresh-runtime E2E using the production XHS adapter with pinned CLI
  shapes: `1 passed`.
- Controlled E2E repeat gate: `5 passed` with `--repeat-each=5`.
- `scripts/verify.ps1`: exit 0; backend `1033 passed, 2 skipped`, Python compile,
  frontend tests/build, controlled E2E, npm audit (`0 vulnerabilities`), tracked
  secret scan and release-boundary scan all passed.
- Unsafe process-boundary scan found no `subprocess.run`, `capture_output=True`,
  `shell=True`, inherited-environment copy, browser-cookie helper or implicit
  login call in the production adapter, guarded live test or controlled E2E
  fixture.
- `git diff --check`: passed. The S2A diff contains no S2B production analysis
  or persistence change, no Bailian implementation change and no `research/`
  file.

## Controlled versus live status

The guarded fake and browser E2E are software verification only. On this host,
the `xhs` executable and every required `XHS_LIVE_*` variable were absent.
Therefore no authenticated account/search command was attempted and real live
status remains exactly:

```text
not_run
```

No live success, authenticated identity, account fact or search fact is claimed.
Independent post-commit review remains required before the whole plan can be
called clean; S2B is still pending.
