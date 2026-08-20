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
