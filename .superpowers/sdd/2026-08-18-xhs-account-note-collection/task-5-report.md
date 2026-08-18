# Task 5 report — operator UI, controlled E2E and live gate

## Scope delivered

- Added strict frontend clients for the Task 3 account collection, keyword search,
  returned-job read, public profile/note read and search-result read routes.
- Account and Radar pages now use single-flight mutations, follow only returned
  job IDs, poll at bounded intervals, cancel scheduled polling on unmount, stop on
  terminal state, and visibly mark failed reads as stale rather than fresh.
- Account collection labels one profile separately from note N/N, preserves every
  submitted job for page-session audit, explains local-session/captcha/rate-limit
  prerequisites, and creates a new job on retry instead of rewriting history.
- Public profile/note facts, source links and canonical `account-note:*` discovery
  IDs are displayed without raw artifacts, credentials, executable paths or local
  absolute paths. The UI explicitly says note trust does not replace shop N/N.
- Keyword search renders only normalized public result fields and exact API N/N.
- The controlled browser fixture has no pre-seeded account-note rows. Each Qianfan
  run generates a unique account; a deterministic provider-neutral adapter then
  enters the production Task 3 service/job/artifact/database path from the UI.
  The browser selects that account's new `account-note:*` ID plus trusted shop
  evidence for analysis before continuing through opportunity, product, content
  approval and an available ZIP.
- Added an explicit opt-in local CLI smoke gate. It uses fixed `status`, `whoami`,
  `user`, `user-posts` and `search` reads only. Missing flag/session/targets report
  `not_run`; access gates retain no successful normalized account facts. It never
  performs login or platform writes and accepts no Cookie, token or password.

## RED evidence

Initial focused frontend run:

```text
npm test -- --run src/api/client.test.ts src/pages/AccountPage.test.tsx src/pages/RadarPage.test.tsx
8 failed, 14 passed
```

The failures showed the six Task 3 client calls, account collection controls,
trusted profile/note display, audit/stale polling and Radar keyword search did not
exist. Existing behaviors stayed green.

The first controlled E2E after adding the required browser actions failed at the
new note boundary:

```text
Locator: getByRole('link', { name: 'Open note source' })
Expected: visible
1 failed
```

That proved the old fixture never produced account notes. After the controlled
adapter was added, E2E exposed a separate integration boundary: downstream product
and content schemas intentionally accept shop/rank evidence, not account-note IDs.
The final controlled model therefore uses account notes to ground analysis claims,
while opportunity/content citations remain bound to exact shop evidence, matching
Task 4's frozen eligibility rule.

## GREEN evidence

```text
npm test --prefix frontend -- --run
8 files passed; 44 tests passed

npm run build --prefix frontend
TypeScript and Vite build exited 0

npm run test:e2e --prefix frontend
1 passed

npm run test:e2e --prefix frontend -- --repeat-each=5
5 passed

python -m pytest backend/tests/xhs backend/tests/analysis -q
428 passed, 2 skipped

powershell -ExecutionPolicy Bypass -File scripts/verify.ps1
1020 backend passed, 2 live skips; 44 frontend passed; build, controlled E2E,
npm audit, secret scan and boundary scan passed
```

The two backend skips are the existing Bailian live opt-in and the new XHS CLI
live opt-in. No authenticated local session was used, so live status remains
truthfully `not_run`.

## Live boundary and concerns

- Controlled fixtures prove software wiring, not current platform authentication,
  live field compatibility or real-source accuracy.
- Live execution requires `XHS_LIVE_TEST=1`, a trusted settings-owned executable,
  an already-authenticated local session, explicit user ID/keyword and exact
  expected counts. No login state is changed by the test.
- Task 3 public note reads do not expose the SQLite note-row ID. The UI therefore
  does not invent a note-to-evidence mapping: it displays normalized public notes
  and the Task 4 `analysis-evidence` canonical IDs as separate trusted facts.
- Account-note evidence remains analysis enrichment. Product/content flows keep
  their existing canonical shop/rank citation contract.

Commit: the single Task 5 commit containing this report is recorded in the final
handoff because a commit cannot contain its own resulting hash.
