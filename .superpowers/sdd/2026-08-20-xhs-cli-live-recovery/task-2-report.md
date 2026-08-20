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
