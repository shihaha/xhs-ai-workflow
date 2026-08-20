# Phase A isolated real UAT report

Date: 2026-08-20 (Asia/Shanghai)

## Verdict

Phase A software and controlled verification are complete, but the real Phase A
business gate is **not complete**. One existing real account remains fully
trusted; no second account could be collected during the bounded top-five
candidate attempt, so no real cross-account candidate was created or reviewed.

## Isolation and evidence reconciliation

- The Stage 2 SQLite database was opened through an online backup into a new
  isolated UAT database. The source database was not modified.
- The authoritative XHS/shop artifacts were reconciled by relative path,
  SHA-256 and physical file identity. The identity-preserving isolated view
  linked 29 required evidence files and passed the existing trust readers.
- The previously completed real Qianfan run was imported through
  `RadarService.ingest_snapshot`: 8 scopes, 80 ranking items and 71 distinct
  ranked-account projections were present.
- No `.env`, Cookie, token, browser profile, phone screenshot/XML, private
  account identifier or runtime path is recorded in this report or Git.

## Existing trusted Stage 2 account

Safe reference: `real-account-A`.

- Public profile: trusted and readable through the production provenance gate.
- Public notes: 62 persisted `account-note:*` facts, all readable through the
  production analysis evidence discovery path.
- Shop result: one trusted `artifact:*` result bound to the account.
- Product verification: expected/discovered/succeeded `2/2/2`, missing `0`,
  `complete=true`, with the previously persisted image-manifest evidence.
- The older real `needs_human` shop task remains historical evidence and was
  not changed or deleted.

## Second-account attempt

The server selected five distinct candidates after excluding `real-account-A`.
The order was: `成交榜 · 优秀账号`, best rank ascending, appearance count
descending, then stable account ID. Best ranks were 1, 2, 4, 5 and 6.

Each candidate was submitted through the existing reserved XHS account
collection path. All five jobs persisted a bounded failure artifact and ended
as `failed/cli_failed` with zero trusted note facts:

- `8b723263-c38e-40ac-90e2-f7e018cfe509`
- `12e6f3e4-177c-48eb-8d28-e5ee00977d2c`
- `88e065ab-9696-40a2-950c-d8e0e492b6d6`
- `9485558f-4229-487c-9f59-2640a1780176`
- `412545f9-edd5-4a22-b5c0-c622767b0fd4`

A bounded control read of `real-account-A` in the same current CLI session also
ended `failed/cli_failed` (`88b67695-814b-4e70-a217-b157d44717b5`). This
separates the current XHS CLI/session failure from candidate ordering. The
temporary copied credential state was removed after every bounded run.

Because no second account reached exact profile + notes, Android collection was
not started for those candidates. This avoids producing unowned shop evidence
or manufacturing a cross-account result.

## Cross-account result

- Trusted real accounts available to clustering: 1.
- Real cross-account analysis calls: 0.
- Real `pending_review` opportunities: 0.
- Human review decisions: 0.
- Real cross-account candidate: **not produced**.

The automated/controlled suite separately proves that two exact accounts yield
`warming_candidate`, three yield `validated_candidate`, review is a one-way
terminal transition, and product creation requires an approved eligible
candidate. Those tests are software evidence and are not reported as real UAT.

## Verification evidence

- Analysis + content focused backend: `458 passed, 1 skipped`.
- Media compatibility focused backend: `57 passed`.
- Frontend unit tests: `8 files, 53 passed`.
- Frontend production build: passed (35 modules).
- Controlled two-account Playwright path: `1 passed`; it performed two exact
  account/shop collections, created a `warming_candidate`, recorded human
  approval, then exercised the existing downstream regression path.
- Final complete backend result is recorded in `PROJECT_STATUS.md` after the
  final clean run.

## Remaining blocker

The only blocker to the real Phase A completion gate is obtaining a second
independent account with exact trusted XHS profile/notes and Android shop N/N
evidence. The current XHS CLI session returns `cli_failed` even for the existing
control account. Phase B remains out of scope and has not started.
