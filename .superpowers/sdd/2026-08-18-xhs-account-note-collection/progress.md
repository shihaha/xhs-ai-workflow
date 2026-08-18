# SDD ledger — plan: docs/superpowers/plans/2026-08-18-xhs-account-note-collection.md

## Preflight

| Tasks / interface | Producer → consumer | Check | Ruling |
|---|---|---|---|
| Task 1 → Task 3 | `XhsCliReadAdapter` → `XhsCollectionService` | Adapter returns provider-neutral `CollectionResult`; service owns jobs/artifacts/DB. | Clean. |
| Task 1 → Task 5 | `Settings.xhs_cli_executable` → operator prerequisite copy | UI must never receive executable/Cookie values. | Clean; health exposes only configured/available facts. |
| Task 2 → Task 3 | account/note records → atomic service finalizer | Schema owns uniqueness/FK; service may write only exact-complete facts. | Clean. |
| Task 2 → Task 4 | note/evidence identity → analysis trust resolver | Analysis must verify producer/path/hash/account ownership rather than trust rows alone. | Clean. |
| Task 3 → Task 4 | reserved job + raw artifact → trusted evidence | Job type, producer and exact artifact contract are load-bearing. | Clean. |
| Task 3 → Task 5 | 202/read APIs → Account/Radar pages | UI follows returned job IDs and never infers completion. | Clean. |
| Task 4 → Task 5 | `account-note:<id>` discovery → analysis UI/E2E | Controlled E2E must use note evidence before analysis. | Clean. |
| Task 1 internal | `fetch_account` must persist one profile and N notes with honest N/N. | The plan's `expected_note_count` does not state whether the profile counts as an item. | Ruling: adapter `fetch_account` returns one profile item plus exactly N note items; service passes `expected_count = expected_note_count + 1`, persists both counts, and UI labels them separately. Cost if wrong: displayed total differs from a user expectation that N refers to notes only, but no fact is hidden or fabricated. |
| Task 2 internal | Migration marker and direct SQL constraints | Marker-present startup is validation-only; marker-absent migration must validate before certification. | Clean. |
| Task 3 internal | Worker close and external process | A blocked subprocess cannot be allowed to hang Python exit or late-finalize success. | Ruling: use a daemon worker plus admission/cancellation fences; subprocess timeout is bounded by Settings. Cost if wrong: an OS child may outlive app shutdown until its timeout, but cannot write a late success. |
| Task 4 internal | Note evidence and opportunity eligibility | Existing opportunity creation still requires trusted complete shop evidence. | Ruling: note evidence enriches/cites analysis but does not replace the shop N/N gate. Cost if wrong: a note-rich account without complete shop evidence remains ineligible for a verified opportunity. |
| Task 5 internal | Live login gate | Controlled fixtures prove software; real local session remains separate. | Clean; no successful live fact without opt-in local login. |

Baseline: `scripts/verify.ps1` exit 0 — backend 643 passed/1 live skip; frontend 34 passed; build; controlled E2E 1/1; npm audit 0; secret and boundary scans clean.

Task 1: fix round 1/5 (2 addressed, 1 open — multilingual exit-zero access-gate message; commits 0dacbab..0bdc838)
Task 1: fix round 2/5 (1 addressed, 0 open — multilingual/structured access gates; commits 0bdc838..8391bfa)
Task 1: complete (commits ccf138f..8391bfa, review clean)
Task 2: fix round 1/5 (4 addressed, 1 new open — owner alias canonicalization; commits 3ffc69e..bda4820)
Task 2: fix round 2/5 (1 addressed, 0 open — conflicting/foreign owner aliases; commits bda4820..78db53e)
Task 2: complete (commits 8391bfa..78db53e, review clean)
Task 3: fix rounds 1-5 completed, then user-approved credential-tokenization stabilization S1 (commits 78db53e..1ccbd49)
Task 3: complete (independent S1 review CLEAN; backend 935 passed/1 live skip; authenticated xhs-cli remains not_run)
Task 4: fix rounds 1-5 completed (commits 87d640c..bb5ea11)
Task 4: complete (final independent review CLEAN; backend 1020 passed/1 live skip; account-note evidence enriches analysis but exact shop N/N gate remains required)
Task 5: fix round 1/5 complete (3 Important addressed; commits b13d452..59e52c3)
Task 5: complete (independent review CLEAN; frontend 47 passed; controlled E2E 1/1 and repeat-5 5/5; repository verify backend 1022 passed/2 live skips; npm audit clean; authenticated real xhs-cli remains not_run)
Whole-plan final review: NOT CLEAN (3 Critical, 1 Important). User authorized continued stabilization.
Stabilization S2A implementation complete; independent post-commit review pending (pinned real shapes, isolated external-auth state, actual credential redaction and bounded child output; controlled verification green; authenticated real xhs-cli remains not_run).
Stabilization S2B pending: append-only/versioned note evidence and post-model pre-commit trust revalidation.
