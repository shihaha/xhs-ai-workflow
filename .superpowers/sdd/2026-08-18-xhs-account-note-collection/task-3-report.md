# Task 3 report — collection service, reserved jobs and API

## Scope delivered

- Commit: `HEAD` (`feat: run durable account note collections`; one Task 3 commit).
- Added a daemon-serial XHS collection service with an admission/close fence,
  restart recovery, bounded shutdown join, scheduling-failure finalization and
  cancellation checkpoints before the external call and before DB finalization.
- Account requests pass `expected_note_count + 1` to `fetch_account`. Exact
  success adds and flushes the reserved raw artifact, calls
  `persist_exact_account_result`, performs the `running -> succeeded` CAS and
  commits all database facts in the same session.
- Search jobs use their own job/artifact types and store a complete normalized
  result in the hash-bound raw artifact. Search reads revalidate path, size,
  SHA-256, job binding, producer, N/N and note-only shape; they never write or
  expose account-owned profile/note rows.
- Added strict HTTP 202 submit routes and public-only profile, account-note and
  search-result reads. Raw evidence and credentials are excluded from read APIs.
- Registered the settings-owned `XhsCliReadAdapter`, wired service lifecycle,
  and reserved both XHS job types/artifact kinds from generic Jobs API mutation.

## RED evidence

Initial focused command:

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py -q
```

Observed expected collection failure:

```text
ModuleNotFoundError: No module named 'backend.app.features.xhs.service'
1 error in 0.57s
```

After the service tests became green, the API/lifecycle tests independently
failed because `app.state.xhs_collection_service` did not exist (`2 failed`),
and the registry test failed because `build_default_registry` did not exist.

Additional security RED probes found real defects before their fixes:

- leaky successful adapter results persisted `secret-sentinel` in both raw
  artifact and normalized raw evidence;
- a malformed adapter return raised `AttributeError` and left the job running;
- a tampered search artifact was returned as a normalized read fact;
- account artifact metadata did not separate note N/N from profile-plus-note
  item N/N.

## GREEN evidence

Focused Task 3 and adjacent jobs/registry tests after implementation:

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py backend/tests/test_adapter_registry.py backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
52 passed in 8.03s
```

Fresh required XHS/jobs focused suite:

```text
python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
115 passed in 9.63s
```

Fresh full backend regression:

```text
python -m pytest backend/tests -q
737 passed, 1 skipped, 32 warnings in 92.26s
```

The one skip is the existing opt-in live gate. The 32 warnings are the existing
Python 3.12 sqlite datetime adapter deprecations in content tests.

## Self-review

- Generic jobs can read and purely cancel the two XHS types, but cannot create,
  claim, append logs, attach artifacts, inject cancellation fields or force a
  successful transition.
- Cancellation that wins before finalization leaves no account facts and no
  artifact row. The already-written uniquely named evidence file is retained as
  an unreferenced recovery artifact because SQLite and NTFS cannot commit
  atomically; it is never returned as durable evidence.
- A lost success commit acknowledgement is resolved by a read-only durable
  state/artifact digest check, so a committed success is not downgraded.
- Adapter outputs receive a second recursive credential-key redaction before
  either artifact or normalized fact persistence. Exception messages are never
  stored; only bounded category and exception class facts are retained.
- The service does not resume queued/running CLI work after restart; it moves it
  to `needs_human/worker_restart_required` for explicit operator action.

## Concerns

- No authenticated live `xhs-cli` invocation was run. Task 5 owns the explicit
  opt-in live gate; current evidence is controlled adapter/subprocess fixtures.
- Search normalized facts are intentionally stored in and read from the single
  immutable, hash-verified artifact because Task 2 introduced relational tables
  only for account profile/note ownership. A future search-history query/index
  requirement would justify a separate migration, but it is outside Task 3.
- External command cancellation is cooperative at the service boundary: a
  running subprocess is bounded by the trusted adapter timeout and its late
  return cannot write success; the service does not attempt unsafe thread or
  process termination.

## Fix round 1/5 — secret shapes, transaction outcome and lifecycle fences

Commit: `HEAD` (`fix: harden xhs collection finalization`; one fix-round commit).

### RED

```text
python -m pytest backend/tests/xhs/test_cli_adapter.py backend/tests/xhs/test_collection_service.py -q -k "structured_header or search_rejects_conflicting or transient_read_failure or shutdown_fence_wins or conflicting_search_owner or historical_search_artifact"
FFFFFFF
7 failed, 62 deselected in 1.21s
```

The failures independently proved all four review findings:

- `headers: [{"name": "Cookie", "value": ...}]` survived both adapter and
  service redaction;
- a committed success whose acknowledgement and next two reads failed was
  followed by a failure payload overwrite of the successful artifact path;
- shutdown could set its fence while account facts were pending, yet the final
  CAS still committed `succeeded` and retained those facts;
- conflicting search `user_id`/`userId` aliases passed adapter normalization,
  service success finalization and historical artifact reads.

### GREEN

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py backend/tests/xhs/test_cli_adapter.py backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
106 passed in 8.77s
```

```text
python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
122 passed in 10.74s
```

```text
python -m pytest backend/tests -q
744 passed, 1 skipped, 32 warnings in 95.37s
```

`python -m compileall -q backend/app` and `git diff --check` exited 0.
The first two full-suite attempts each had only the existing shop asynchronous
POST timing assertion above its 0.2 second wall-clock threshold (0.219 and
0.203 seconds); that test passed alone, no shop code was changed, and the fresh
third full run above passed all non-live tests.

### Fixes and self-review

- Adapter and service now share one recursive redactor. It recognizes direct
  credential keys and semantic `name`/`value(s)` header entries while retaining
  ordinary entries such as `{"name": "title", "value": ...}`.
- Failure evidence uses a distinct `-failure.json` path and performs a durable
  running-state preflight before any write. If transaction outcome/readback is
  unknown it writes nothing; a later confirmed success is returned without
  touching its bound bytes.
- Final job CAS and commit now hold the same lifecycle lock that owns the
  shutdown admission fence. Once close has set the fence, pending artifact and
  account fact writes roll back and the close path can persist cancellation.
- Search normalization, service finalization and historical search reads all
  use `canonical_owner_id`; conflicting or malformed retained aliases fail
  closed instead of selecting the first field.

### Remaining live boundary

No authenticated live `xhs-cli` command was run. The existing live gate remains
Task 5 scope and is still truthfully `not_run` here.

## Fix round 2/5 — lifecycle lock order and exact credential names

Commit: `HEAD` (`fix: remove xhs collection lock inversion`; one fix-round
commit).

### RED

Barrier-based regression tests reproduced the review findings before the
implementation changed:

```text
python -m pytest backend/tests/xhs/test_cli_adapter.py::test_structured_header_name_value_credentials_are_redacted_without_false_positive backend/tests/xhs/test_collection_service.py -q -k "structured_header or lifecycle_lock_while_waiting or close_fences_a_submit or two_normal_submits"
3 failed, 2 passed
```

- a finalizer holding SQLite could not reacquire the lifecycle lock while a
  concurrent submit held that lock waiting for SQLite; it missed the one-second
  barrier and the submit later reached SQLite's lock timeout;
- close could not set its admission fence while a submit was paused inside the
  database create call;
- substring credential matching removed ordinary `session title` and
  `secret garden` values. The normal two-submit characterization already
  passed and remained part of the concurrency gate.

An additional strict RED assertion showed `access_token` still leaked after
the first exact-name implementation; adding its normalized exact name made that
test green without broadening matching back to substrings.

### GREEN

The four lifecycle interleavings pass together:

```text
python -m pytest backend/tests/xhs/test_collection_service.py -q -k "lifecycle_lock_while_waiting or close_fences_a_submit or shutdown_fence_wins or two_normal_submits"
4 passed, 16 deselected in 0.87s
```

Fresh Task 3 focused suite:

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py backend/tests/xhs/test_cli_adapter.py backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
109 passed in 9.14s
```

Fresh required XHS/jobs suite:

```text
python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
125 passed in 11.13s
```

Fresh full backend regression:

```text
python -m pytest backend/tests -q
747 passed, 1 skipped, 32 warnings in 90.69s
```

The skip remains the opt-in live gate; the warnings remain the existing Python
3.12 sqlite datetime adapter deprecations. `python -m compileall -q backend/app`
and `git diff --check` exited 0. This full run had no shop timing failure and no
shop test or implementation was changed.

### Fixes and self-review

- Submit now holds the lifecycle condition only while acquiring an admission
  generation/token and maintaining its active-admission count. Job creation,
  cancellation and executor submission happen without the lifecycle lock.
- Close first closes admission and increments the generation, then waits on a
  condition (which releases the lifecycle lock) for in-flight admissions to
  reconcile. A reservation created across the fence is cancelled and its
  caller receives `CollectionServiceClosed`; an accepted submit is registered
  before close snapshots/cancels futures.
- Final success keeps the existing SQLite-to-short-lifecycle-lock order. No
  lifecycle-lock-to-SQLite path remains, while the generation fence preserves
  the rule that close wins over late success and account facts roll back.
- Credential redaction now uses an exact, case-folded, underscore-normalized
  allowlist for header/credential names. `Cookie`, `Authorization`, `token`,
  `access_token` and the other enumerated credential names are removed, while
  ordinary semantic names containing words such as session or secret survive.

### Remaining live boundary

No authenticated live `xhs-cli` command was run. Task 5 still owns that
explicit opt-in gate, so the live boundary remains `not_run`.

## Fix round 3/5 — credential alias canonicalization

Commit: `HEAD` (`fix: redact xhs credential aliases`; one fix-round commit).

### RED

The new tests exercised both direct dictionary keys and structured
`{"name": ..., "value": ...}` fields through the real CLI adapter, plus
account/search adapter-to-service persistence:

```text
python -m pytest backend/tests/xhs/test_cli_adapter.py::test_direct_and_structured_credential_aliases_are_redacted backend/tests/xhs/test_cli_adapter.py::test_structured_header_name_value_credentials_are_redacted_without_false_positive backend/tests/xhs/test_collection_service.py::test_real_adapter_account_alias_credentials_never_reach_artifact_or_facts backend/tests/xhs/test_collection_service.py::test_real_adapter_search_alias_credentials_never_reach_artifact_or_read_facts -q
11 failed, 5 passed in 0.81s
```

The failures proved that camelCase `accessToken`/`refreshToken`, canonical
`auth_token` aliases and `cookie_string` aliases reached normalized adapter
evidence and durable artifacts. Exact names already recognized by round 2 and
the ordinary `session title`/`secret garden` values remained passing controls.

### GREEN

Targeted alias and end-to-end persistence regression:

```text
16 passed in 0.58s
```

Fresh Task 3 focused suite:

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py backend/tests/xhs/test_cli_adapter.py backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
124 passed in 9.22s
```

Fresh required XHS/jobs suite:

```text
python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
140 passed in 11.57s
```

Fresh full backend regression:

```text
python -m pytest backend/tests -q
762 passed, 1 skipped, 32 warnings in 93.04s
```

The skip remains the opt-in live gate and the warnings remain the existing
Python 3.12 sqlite datetime adapter deprecations. `python -m compileall -q
backend/app` and `git diff --check` exited 0.

### Fixes and self-review

- Credential names are split at acronym/camelCase boundaries, separator runs
  are normalized to hyphens and comparison is case-folded.
- The normalized result is still checked only against a finite exact allowlist;
  `auth-token` and `cookie-string` were added as deliberate credential aliases.
  There is no substring rule, so names merely containing `session` or `secret`
  remain ordinary content.
- Direct and structured variants now cover camelCase, snake_case, kebab-case
  and space-separated forms including `accessToken`, `refreshToken`,
  `auth_token`, `cookie_string`, `clientSecret` and `apiKey`.
- Real account collection tests prove the secret is absent from the artifact,
  profile fact and note fact. Real search collection tests prove it is absent
  from the independent search artifact and normalized read facts.

### Remaining live boundary

No authenticated live `xhs-cli` command was run. Task 5 still owns that
explicit opt-in gate, so the live boundary remains `not_run`.

## Fix round 4/5 — canonical credential-name classification

Commit: `HEAD` (`fix: classify xhs credential names`; one fix-round commit).

### RED

The new regression matrix exercised canonical credential aliases as direct
dictionary keys and structured `{"name": ..., "value": ...}` entries through
the real CLI adapter. Account and search cases then carried the four reported
aliases through the real adapter-to-service persistence boundary:

```text
python -m pytest backend/tests/xhs/test_cli_adapter.py::test_direct_and_structured_credential_aliases_are_redacted backend/tests/xhs/test_cli_adapter.py::test_noncredential_full_token_sequences_are_preserved backend/tests/xhs/test_collection_service.py::test_real_adapter_account_alias_credentials_never_reach_artifact_or_facts backend/tests/xhs/test_collection_service.py::test_real_adapter_search_alias_credentials_never_reach_artifact_or_read_facts -q
10 failed, 25 passed in 0.73s
```

The failures proved that `csrfToken` (including snake, kebab and spaced
forms), `xsrfToken`, `bearerToken`, `jwtToken` and `sessionCookie` survived as
both direct and structured credentials. The account/search persistence tests
proved those values reached durable raw artifacts. All ordinary full-token
controls passed, including `session title`, `secret garden`, `tokenCount`,
`access level`, `refresh rate`, `api key note` and `client secret garden`.

### GREEN

Targeted alias, false-positive and real persistence regression:

```text
46 passed in 0.67s
```

Lifecycle, shutdown, commit-acknowledgement and owner-conflict lock:

```text
python -m pytest backend/tests/xhs/test_collection_service.py -q -k "lifecycle_lock_while_waiting or close_fences_a_submit or shutdown_fence_wins or commit_ack or conflicting_search_owner or historical_search_artifact or two_normal_submits"
8 passed, 14 deselected in 1.39s
```

Fresh Task 3 focused suite:

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py backend/tests/xhs/test_cli_adapter.py backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
155 passed in 9.18s
```

Fresh required XHS/jobs suite:

```text
python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
171 passed in 11.23s
```

Fresh full backend regression:

```text
python -m pytest backend/tests -q
793 passed, 1 skipped, 32 warnings in 90.07s
```

The skip remains the opt-in live gate and the warnings remain the existing
Python 3.12 sqlite datetime adapter deprecations. `python -m compileall -q
backend/app` and `git diff --check` exited 0.

### Fixes and self-review

- Credential names are canonicalized into complete, case-folded token tuples
  after camelCase/acronym and separator splitting.
- One `_is_credential_name` predicate now classifies both direct keys and
  structured semantic names. It uses finite exact credential terms, explicit
  two-token combinations, finite token/cookie qualifiers and an optional
  canonical `x` header prefix.
- Recognized families include cookie, auth/authorization, access/refresh,
  CSRF/XSRF, bearer/JWT, API key, client secret and session cookie/token.
  Previous access/refresh/auth/cookie/client-secret/API-key aliases remain in
  the real persistence regressions.
- Classification consumes the complete token sequence. It has no substring
  fallback, so extra ordinary tokens do not trigger redaction.
- No adapter command, service transaction, lifecycle, shutdown,
  commit-acknowledgement or owner-binding control flow changed in this round.

### Remaining live boundary

No authenticated live `xhs-cli` command was run. Task 5 still owns that
explicit opt-in gate, so the live boundary remains `not_run`.

## Fix round 5/5 — finite credential grammar

Commit: `HEAD` (`fix: define xhs credential grammar`; one fix-round commit).

### RED

The new positive/negative matrix exercises each name as both a direct mapping
key and a structured `{"name": ..., "value": ...}` entry through the real
adapter. The account and search cases then carry the reported names across the
real adapter-to-service persistence boundary:

```text
python -m pytest backend/tests/xhs/test_cli_adapter.py::test_direct_and_structured_credential_aliases_are_redacted backend/tests/xhs/test_cli_adapter.py::test_noncredential_full_token_sequences_are_preserved backend/tests/xhs/test_collection_service.py::test_real_adapter_account_alias_credentials_never_reach_artifact_or_facts backend/tests/xhs/test_collection_service.py::test_real_adapter_search_alias_credentials_never_reach_artifact_or_read_facts -q --tb=short
17 failed, 51 passed in 0.87s
```

The failures proved both sides of the classifier defect:

- `apiToken`, `oauthToken`, `personalAccessToken`, `apiSecret`, `sessionId`,
  `webSession`/`web_session`, `x-api-token`, `x-oauth-token` and
  `x-session-id` survived direct and structured redaction;
- ordinary single-token `access`, `refresh` and `session` fields were removed;
- the new aliases reached the real account artifact/profile/note facts and the
  real search artifact/read boundary.

### GREEN

Targeted grammar and real persistence regression:

```text
68 passed in 0.62s
```

Lifecycle lock order, shutdown fence, commit acknowledgement and owner
conflict regression:

```text
python -m pytest backend/tests/xhs/test_collection_service.py -q -k "lifecycle_lock_while_waiting or close_fences_a_submit or shutdown_fence_wins or commit_ack or conflicting_search_owner or historical_search_artifact or two_normal_submits"
8 passed, 14 deselected in 1.48s
```

Fresh Task 3 focused suite, including the adapter registry:

```text
python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py backend/tests/xhs/test_cli_adapter.py backend/tests/test_adapter_registry.py backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
180 passed in 9.44s
```

Fresh required XHS/jobs suite:

```text
python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
193 passed in 11.37s
```

Fresh full backend regression:

```text
python -m pytest backend/tests -q
815 passed, 1 skipped, 32 warnings in 86.36s
```

The first two full-suite attempts each had only the existing shop asynchronous
POST wall-clock assertion above its 0.2 second threshold (0.25 and 0.235
seconds). That test passed alone (`1 passed in 0.69s`), no shop code changed,
and the fresh third full run above passed every non-live test. The skip remains
the opt-in live gate and the warnings remain the existing Python 3.12 sqlite
datetime adapter deprecations. `python -m compileall -q backend/app` and
`git diff --check` exited 0.

### Fixes and self-review

- Credential names still normalize camelCase, acronym boundaries and separator
  runs into complete, case-folded token tuples.
- Single-token recognition is limited to explicit credential cores such as
  token, cookie, authorization, JWT, CSRF/XSRF, password and secret. `access`,
  `refresh` and `session` are no longer sensitive without a credential carrier.
- Multi-token recognition is a finite grammar: approved qualifier sequences
  can terminate in `token`, while a carrier-to-qualifier map covers session ID,
  web session, API key/secret, client secret, cookie forms and proxy
  authorization. `personal access token` is the one approved multi-qualifier
  token chain.
- One optional canonical `x` prefix is removed before applying the same grammar.
  Direct dictionary keys and structured semantic names call the same predicate.
- The complete token sequence must match, so ordinary `tokenCount`,
  `session title`, `secret garden`, `api key note`, and related controls remain
  intact. No adapter command, service transaction, lifecycle or owner-binding
  control flow changed.

### Remaining live boundary

No authenticated live `xhs-cli` command was run. Task 5 still owns that
explicit opt-in gate, so the live boundary remains `not_run`.

## Stabilization S1 — identifier tokenization and credential grammar

Commit: `HEAD` (`fix: stabilize xhs credential tokenization`; one independent
Task 3 stabilization commit after the five review rounds).

### RED

The new table-driven tests separate identifier tokenization from credential
grammar, then exercise the same classifier through direct mapping keys,
structured `{"name": ..., "value": ...}` entries, the real CLI adapter and
the account/search persistence boundary:

```text
python -m pytest backend/tests/xhs/test_redaction.py backend/tests/xhs/test_cli_adapter.py::test_direct_and_structured_credential_aliases_are_redacted backend/tests/xhs/test_cli_adapter.py::test_noncredential_full_token_sequences_are_preserved backend/tests/xhs/test_collection_service.py::test_real_adapter_account_alias_credentials_never_reach_artifact_or_facts backend/tests/xhs/test_collection_service.py::test_real_adapter_search_alias_credentials_never_reach_artifact_or_read_facts -q --tb=short
62 failed, 126 passed in 0.87s
```

The RED run confirmed the root cause before production code changed:

- `OAuthToken` tokenized as `('o', 'auth', 'token')`;
- `XOAuthToken` tokenized as `('xo', 'auth', 'token')`;
- `XCSRFToken` tokenized as `('xcsrf', 'token')`;
- `XAPIKey` tokenized as `('xapi', 'key')`;
- those names and `URLToken` failed direct and structured redaction, and their
  unique sentinels reached real account/search artifacts and normalized facts.

### GREEN

The same targeted tokenizer, grammar, adapter and persistence command passed:

```text
188 passed in 0.61s
```

Fresh Task 3 focused suite, including the standalone redaction tests and
adapter registry:

```text
python -m pytest backend/tests/xhs/test_redaction.py backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py backend/tests/xhs/test_cli_adapter.py backend/tests/test_adapter_registry.py backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
300 passed in 8.63s
```

Lifecycle lock order, shutdown fence, commit acknowledgement and owner
conflict regression:

```text
python -m pytest backend/tests/xhs/test_collection_service.py -q -k "lifecycle_lock_while_waiting or close_fences_a_submit or shutdown_fence_wins or commit_ack or conflicting_search_owner or historical_search_artifact or two_normal_submits"
8 passed, 14 deselected in 1.33s
```

Fresh required XHS/jobs suite:

```text
python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q
313 passed in 10.72s
```

Fresh full backend regression:

```text
python -m pytest backend/tests -q
935 passed, 1 skipped, 32 warnings in 85.37s
```

The skip remains the existing opt-in live gate and the warnings remain the
existing Python 3.12 sqlite datetime adapter deprecations. `python -m
compileall -q backend/app` and `git diff --check` exited 0.

### Stabilization and self-review

- Identifier tokenization now has its own literal table for lower camel case,
  Pascal case, uppercase acronym-plus-suffix identifiers, snake/kebab/space
  separators and joined/explicit `x` prefixes.
- The tokenizer preserves lexical acronym boundaries (`OAuth`, API, CSRF/XSRF,
  JWT, URL and ID) without enumerating complete credential aliases. The
  credential classifier consumes the resulting complete token tuple through a
  separate finite grammar; `url` is an approved token qualifier.
- `OAuthToken`, `XOAuthToken`, `XCSRFToken`, `XAPIKey`, `APIKey`, `CSRFToken`,
  `JWTToken`, `URLToken`, `AccessToken`, `RefreshToken`,
  `PersonalAccessToken`, `SessionID`, `WebSession` and every earlier alias are
  covered as both direct and structured names.
- Negative full-token controls retain `access`, `refresh`, `session`,
  `session title`, `secret garden`, `tokenCount`, `api response`,
  `oauth display name`, ordinary `title` and ordinary `body`; there is no
  substring fallback.
- Direct and structured redaction still share `_is_credential_name`. Real
  `XhsCliReadAdapter -> XhsCollectionService` account and search tests verify
  unique secret sentinels are absent from the raw artifact, profile fact, note
  fact and normalized search read fact.
- No adapter command, service transaction, lifecycle, shutdown,
  commit-acknowledgement or owner-binding control flow changed in S1.

### Remaining live boundary

No authenticated live `xhs-cli` command was run. The explicit live boundary is
still truthfully `not_run`.
