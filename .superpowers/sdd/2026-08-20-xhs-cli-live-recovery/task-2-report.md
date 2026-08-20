# Task 2: bounded `user-posts` page-slot recovery

## Scope

- Changed only `backend/app/adapters/xhs_cli_readonly_wrapper.py` and its
  focused test `backend/tests/xhs/test_xhs_cli_readonly_wrapper.py`.
- The wrapper still accepts only its existing fixed command allowlist, verified
  source hashes, and stdin-only prepared cookies. No request URL, selector, or
  caller-provided JavaScript is accepted.
- The supplement is installed only for the already-validated `user-posts`
  command. It calls the verified-memory client's original read, then performs
  at most three fixed `1200px` page scrolls with fixed `1000ms` waits. The
  first no-growth observation stops the loop, so its maximum additional wait is
  three seconds, below the existing 20-second child-command budget.

## Root cause

Pinned `xhs-cli` `XhsClient.get_user_posts` waits only until
`window.__INITIAL_STATE__.user.notes` is any list/object and immediately
extracts it. The observed five page slots therefore returned
`[32, 0, 0, 0, 0]` as a terminal payload even though later read-only scrolling
can grow the same state to the historically observed complete account set.

## TDD evidence

### RED

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py -q
```

Before the production change: `3 failed, 1 passed`. The controlled FakePage
started with five slots `[32, 0, 0, 0, 0]`; the old wrapper returned the nested
initial slots, rather than the expected flattened 62-note maximum collection.
The no-growth and duplicate/order tests failed for the same missing behavior.
The non-`user-posts` isolation test already passed.

### GREEN and focused regression

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py backend/tests/xhs/test_cli_adapter.py backend/tests/xhs/test_s2a_round1_hardening.py backend/tests/xhs/test_s2a_round2_hardening.py -q
```

Result: `186 passed in 10.27s`.

An additional combined run that included `test_live_cli_contract.py` was not a
wrapper regression gate: two existing fake-live setup tests failed before the
wrapper process started because the current repository `.env` supplies an
`XHS_CLI_STATE_DIR` outside pytest's temporary `runtime_dir`, which Settings
correctly rejects. The failure was
`xhs_cli_state_dir must be isolated inside runtime_dir`; it is outside this
task's permitted files and behavior.

The direct wrapper tests cover:

- page-slot growth from the real initial shape to a 62-note de-duplicated set;
- no growth retaining the observed 32-note partial rather than fabricating rows;
- repeated IDs preserving first-observed order without count inflation; and
- no method replacement for non-`user-posts` commands.

## Behavior retained

The wrapper does not write platform state, use credentials outside stdin, alter
the allowlist or pinned package verification, change ownership checks, or
claim completion for a partial result. When scrolling adds nothing, it returns
the real partial set so the existing upper-layer exact-count logic remains
responsible for `needs_human`.

## Round 1: delayed page-slot timing recovery

### Root cause

A real control reproduction observed 122 notes during an unknown-total probe,
then only 62/122 during the immediately following exact persistence read. The
first fixed scroll can leave `user.notes` unchanged while a later fixed scroll
loads the next slot. Treating that first no-growth read as terminal preserved a
timing-dependent partial result.

### TDD evidence

RED command:

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py -q -k first_follow_up
```

Before the round-1 production change: `1 failed, 4 deselected`. The controlled
five-slot FakePage remained at 32 rows after the first fixed scroll, grew to
62 rows after the second, and the previous early-stop behavior returned only
the first 32 rows.

GREEN commands:

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py -q
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py backend/tests/xhs/test_cli_adapter.py backend/tests/xhs/test_s2a_round1_hardening.py backend/tests/xhs/test_s2a_round2_hardening.py -q
```

Results: `5 passed in 0.02s`; `187 passed in 10.08s`.

The bounded supplement now completes all three fixed one-second read-only
scroll attempts unless browser interaction/evaluation raises. No-growth still
returns only the actually observed partial collection; it no longer stops the
second and third timing-safe checks.

## Round 2: final lazy-slot timing recovery

### Root cause

A real exact retry improved from 62 to 92 notes but remained
`needs_human` against the prior 122-note observation. This showed the fixed
three-attempt cap could still end before the final lazy-loaded slot appeared.

### TDD evidence

RED command:

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py -q -k all_five_fixed_scrolls
```

Before the round-2 production change: `1 failed, 5 deselected`. The controlled
five-slot FakePage had two initial no-growth reads, then grew from 32 to 62,
92, and finally 122 rows on the third through fifth scrolls. The three-scroll
cap returned only 62 rows.

GREEN commands:

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py -q
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py backend/tests/xhs/test_cli_adapter.py backend/tests/xhs/test_s2a_round1_hardening.py backend/tests/xhs/test_s2a_round2_hardening.py -q
```

Results: `6 passed in 0.04s`; `188 passed in 10.19s`.

The fixed bounded cap is now five one-second attempts (maximum additional
wait: five seconds). There is no adaptive behavior, no unbounded retry, and
no change to exception stopping, read-only browser use, ID de-duplication, or
partial-result accounting.

## Round 3: bounded stable-read completion

### Root cause

Five real candidates showed that even the five-scroll cap could keep growing
(the largest reached 182 observed notes); another candidate timed out. A fixed
cap alone could not distinguish a genuinely complete observation from a still
loading final slot.

### TDD evidence

RED command:

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py -q -k 'past_five_growths or hard_twenty'
```

Before the round-3 production change: `2 failed, 6 deselected`. The first
FakePage continued growing beyond five scrolls to 182 rows before two stable
reads; the old cap returned 172. The second grew on every read and proved the
old cap could not exercise the requested exact 20-attempt ceiling.

GREEN commands:

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py -q
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py backend/tests/xhs/test_cli_adapter.py backend/tests/xhs/test_s2a_round1_hardening.py backend/tests/xhs/test_s2a_round2_hardening.py -q
```

Results: `8 passed in 0.04s`; `190 passed in 9.96s`.

The final mechanism has a hard maximum of 20 fixed one-second attempts
(maximum additional wait: 20 seconds). A growth resets the stability counter;
only two consecutive no-growth reads stop early, so one transient empty window
is tolerated. Browser interaction/evaluation exceptions still stop immediately.
It remains read-only, keeps first-observed ID order, and returns only actually
observed partial rows when exact-count completion is not available.

## Round 4: default child-budget alignment

### Root cause

The final wrapper permits an initial trusted `get_user_posts` read followed by
up to 20 seconds of fixed read-only scroll waits, while the Settings default
for the enclosing child process remained 20 seconds. The process could timeout
before the bounded wrapper had a chance to reach either its stable-read stop or
its hard cap.

### TDD evidence

RED command:

```powershell
python -m pytest backend/tests/test_settings.py -q -k default_xhs_cli_budget
```

Before the round-4 production change: `1 failed, 5 deselected`; the default
was `20.0`, not the required `60.0` and not greater than the 20-second initial
read budget plus the wrapper's 20-second fixed scroll window.

GREEN commands:

```powershell
python -m pytest backend/tests/test_settings.py -q
python -m pytest backend/tests/test_settings.py backend/tests/xhs/test_xhs_cli_readonly_wrapper.py backend/tests/xhs/test_cli_adapter.py backend/tests/xhs/test_s2a_round1_hardening.py backend/tests/xhs/test_s2a_round2_hardening.py -q
```

Results: `6 passed in 0.12s`; `196 passed in 9.93s`.

`Settings` now defaults `xhs_cli_timeout_seconds` to 60 seconds, retaining the
existing 120-second maximum. A no-network Settings-to-adapter integration test
proves that the default reaches the adapter and exceeds the initial read plus
maximum fixed-scroll wait budget. Existing non-environment Settings unit tests
now explicitly disable dotenv loading, so the current external prepared-state
path cannot make temporary-runtime tests fail before their assertions. The
repository `.env` was not changed.

## Round 5: worst-case child-budget margin

### Root cause

The 60-second default covered only a simplified initial-read estimate. The
reviewed worst case is 20 seconds for the profile goto, up to 3 seconds for its
fixed startup wait, 15 seconds for data readiness, and 20 seconds for bounded
scroll waits: 58 seconds before child startup and output parsing overhead.

### TDD evidence

RED command:

```powershell
python -m pytest backend/tests/test_settings.py -q -k default_xhs_cli_budget
```

Before the round-5 production change: `1 failed, 5 deselected`; the Settings
default was `60.0`, not the required `90.0`.

GREEN commands:

```powershell
python -m pytest backend/tests/test_settings.py -q
python -m pytest backend/tests/test_settings.py backend/tests/xhs/test_xhs_cli_readonly_wrapper.py backend/tests/xhs/test_cli_adapter.py backend/tests/xhs/test_s2a_round1_hardening.py backend/tests/xhs/test_s2a_round2_hardening.py -q
```

Results: `6 passed in 0.12s`; `196 passed in 10.22s`.

The default timeout is now 90 seconds. The no-network Settings-to-adapter
budget test explicitly accounts for `20 + 3 + 15 + 20` seconds and requires a
fixed at-least-10-second startup/parse margin while retaining the existing
120-second maximum. No wrapper, environment, or other module changed.

## Round 6: API-limit-compatible scroll cap

### Root cause

In a fresh real control session, two unknown-total read-only observations grew
from 542 to 662 notes. The second observation still grew through the existing
20-scroll cap, so that cap did not align with the adapter's permitted
1000-note collection boundary.

### TDD evidence

RED command:

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py -q -k allowed_thousand
```

Before the round-6 production change: `1 failed, 8 deselected`. A controlled
initial 542-note slot continued growing over 40 fixed reads to the allowed
1000-note boundary; the 20-scroll cap stopped at about 771 rows.

GREEN commands:

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py -q
python -m pytest backend/tests/test_settings.py backend/tests/xhs/test_xhs_cli_readonly_wrapper.py backend/tests/xhs/test_cli_adapter.py backend/tests/xhs/test_s2a_round1_hardening.py backend/tests/xhs/test_s2a_round2_hardening.py -q
```

Results: `9 passed in 0.03s`; `197 passed in 10.23s`.

The hard cap is now 40 fixed one-second scroll attempts. This is explicitly
finite and does not assume a particular number of notes per page: it permits a
542-note initial slot to reach the allowed 1000-note boundary in the controlled
continuous-growth case. The existing 90-second default remains budget-safe:
`20 + 3 + 15 + 40 + 10 = 88` seconds. The two-consecutive-no-growth early
stop, exception stop, read-only boundary, ID de-duplication, ordering, and
partial-result behavior remain unchanged.

## Round 7: terminal bounded collection limit

### Root cause

The direct unknown-total control reached 1262 unique notes with no rejected
rows, consumed the 40 one-second attempts, and was still growing. Returning
that snapshot as if it had naturally stabilized would make an incomplete
bounded observation look authoritative. It also exceeded the previous
account-note API limit of 1000.

### TDD evidence

RED commands:

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py -q -k sixtieth
python -m pytest backend/tests/test_settings.py -q -k default_xhs_cli_budget
python -m pytest backend/tests/xhs/test_collection_api.py -q -k two_thousand
python -m pytest backend/tests/xhs/test_cli_adapter.py -q -k bounded_limit_is
```

Before the production change, the settings assertion found `90.0` instead of
`120.0`; the API rejected 2000 account notes; and a `bounded_collection_limit`
child result was reduced to `failed/cli_failed` rather than `needs_human`. The
new wrapper test initially exposed a missing test import, corrected before the
production edit; its post-change result is recorded below.

GREEN commands:

```powershell
python -m pytest backend/tests/xhs/test_xhs_cli_readonly_wrapper.py backend/tests/xhs/test_cli_adapter.py -q
python -m pytest backend/tests/test_settings.py -q -k default_xhs_cli_budget
python -m pytest backend/tests/xhs/test_collection_api.py -q -k two_thousand
python -m pytest backend/tests/xhs/test_collection_service.py -q -k two_thousand
```

Results: wrapper and adapter `159 passed`; focused settings `1 passed`; API
schema boundary `1 passed`; service boundary `1 passed`; the complete service
file `27 passed`; and the combined wrapper/adapter/settings/service focused
suite `192 passed`.

The wrapper now permits at most 60 fixed one-second reads. It retains its
two-consecutive-no-growth early stop and immediate exception stop, but raises
the explicit `bounded_collection_limit` category when read 60 itself adds an
unseen ID. The wrapper allowlists that category and the adapter preserves it as
`needs_human`, never as `cli_failed` or complete data. Collection remains
read-only with first-seen ID de-duplication and stable order.

Account collection accepts up to 2000 notes (the adapter accepts 2001 items to
include the profile), while search remains capped at 1000. The default child
timeout is 120 seconds; its no-network budget test covers
`20 + 3 + 15 + 60 + 10 = 108` seconds, within the existing 120-second setting
maximum. No dotenv, runtime, credentials, Phase B, analysis, or research file
was changed.
