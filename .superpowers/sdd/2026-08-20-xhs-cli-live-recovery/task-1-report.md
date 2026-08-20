# Task 1: profile-timeout recovery

## Scope

- Changed only `backend/app/adapters/xhs_cli_read.py` and
  `backend/tests/xhs/test_cli_adapter.py`.
- Added `timeout` to the existing current-profile fallback failure category.
- Did not change command timeouts, ownership checks, exact-count accounting,
  credential redaction, trust rules, database behavior, or `user-posts` failure
  handling.

## TDD evidence

### RED

Command:

```powershell
python -m pytest backend/tests/xhs/test_cli_adapter.py -q -k fetch_account_uses_consistent_post_authors_after_profile_timeout
```

Result before the production change: `1 failed, 146 deselected`. The controlled
runner raised `XhsCliReadError("timeout")` for `user`, made `whoami` unavailable,
and returned 62 consistent-author public post rows. `fetch_account` returned
`('failed', False)` instead of the required exact successful 63-item profile plus
notes result.

### GREEN

The same command passed after adding `timeout` to the existing profile fallback
category: `1 passed, 146 deselected`.

Focused adapter regression command:

```powershell
python -m pytest backend/tests/xhs/test_cli_adapter.py -q
```

Result: `147 passed in 1.29s`.

## Behavior retained

`timeout` is accepted only while recovering the failed `user` profile. The
separate `user-posts` `XhsCliReadError` handler remains unchanged and still
returns a failed result. Existing focused tests cover the adapter's ownership,
exact-count, and credential-redaction behavior.
