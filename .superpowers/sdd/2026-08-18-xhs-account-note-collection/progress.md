# SDD ledger — plan: docs/superpowers/plans/2026-08-18-xhs-account-note-collection.md

## Preflight

| Tasks / interface | Producer → consumer | Check | Ruling |
|---|---|---|---|
| Task 1 → Task 3 | `XhsCliReadAdapter` → `XhsCollectionService` | Adapter returns provider-neutral `CollectionResult`; service owns jobs/artifacts/DB. | Clean. |
| Task 1 → Task 5 | `Settings.xhs_cli_python_executable` → pinned read-only wrapper | UI must never receive interpreter/Cookie values. | Clean; the interpreter is trusted Settings state, while credentials use only the wrapper stdin channel. |
| Task 2 → Task 3 | account/note records → atomic service finalizer | Schema owns uniqueness/FK; service may write only exact-complete facts. | Clean. |
| Task 2 → Task 4 | note/evidence identity → analysis trust resolver | Analysis must verify producer/path/hash/account ownership rather than trust rows alone. | Clean. |
| Task 3 → Task 4 | reserved job + raw artifact → trusted evidence | Job type, producer and exact artifact contract are load-bearing. | Clean. |
| Task 3 → Task 5 | 202/read APIs → Account/Radar pages | UI follows returned job IDs and never infers completion. | Clean. |
| Task 4 → Task 5 | `account-note:<id>` discovery → analysis UI/E2E | Controlled E2E must use note evidence before analysis. | Clean. |
| Task 1 internal | `fetch_account` must persist one profile and N notes with honest N/N. | The plan's `expected_note_count` does not state whether the profile counts as an item. | Ruling: adapter `fetch_account` returns one profile item plus exactly N note items; service passes `expected_count = expected_note_count + 1`, persists both counts, and UI labels them separately. Cost if wrong: displayed total differs from a user expectation that N refers to notes only, but no fact is hidden or fabricated. |
| Task 2 internal | Migration marker and direct SQL constraints | Marker-present startup is validation-only; marker-absent migration must validate before certification. | Clean. |
| Task 3 internal | Worker close and external process tree | A blocked CLI or spawned browser cannot outlive shutdown or late-finalize success. | Ruling: service close fences admission and signals adapter cancellation; Windows creates the process atomically inside a kill-on-close Job Object and owns all pipes/handles through bounded cleanup. Lifespan fails loudly if the 0.25-second safe-close contract is not met. |
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
Stabilization S2A fix round 1/5 complete in this commit after review RED 12/12: pinned read-only wrapper and stdin credentials; handle-pinned/reparse-and-hardlink-rejecting state; Windows whole-tree Job Object lifecycle; 0.25-second service-to-runner cancellation; one shared raw response with linear item evidence; adapter/result artifact hard caps. Final hardening 17 passed; focused 374 passed/1 skipped; repository verify 1050 passed/2 skipped; controlled E2E repeat-5 5/5; authenticated live remains not_run.
Stabilization S2A fix round 2/5 complete in this commit: all five pinned package sources are hash-bound to in-memory execution with no package import/pyc fallback; credential paths and private runtime are parent-handle-relative; every post-CreateProcess failure uses bounded whole-Job cleanup and raw-fd pipe ownership; result/failure artifacts use unique staging, authorized promotion, commit-ack identity checks and staging-only cleanup. Dedicated round-2 hardening 17 passed; focused 395 passed/1 skipped; analysis 90 passed/1 skipped; repository verify 1072 passed/2 skipped; controlled E2E repeat-5 5/5; authenticated live remains not_run.
Stabilization S2A fix round 3/5 complete in this commit: durable prepared/promoted/completed artifact-promotion journal with validation-only physical migration checks; committed/rolled-back/unknown acknowledgement classification; journal-only idempotent startup reconciliation; and handle-bound single-identity staging creation, fsync, promotion, recovery, demotion and cleanup on Windows/POSIX. Dedicated round-3 hardening 18 passed; focused 413 passed/1 skipped; analysis 90 passed/1 skipped; repository verify 1090 passed/2 skipped; controlled E2E repeat-5 5/5; authenticated live remains not_run.
Stabilization S2A fix round 4/5 complete in this commit: unified fail-closed formal-read provenance; seven strict bidirectional journal/parent physical guards; v2 durable allocating intent with populated v1 migration; DB-clock cross-process CAS recovery leases and active-finalizer protection; and POSIX atomic no-replace promotion with fail-closed deletion plus Windows held-handle regression coverage. Dedicated round-4 22 passed three times; focused 435 passed/1 skipped; schema migration/hardening 63 passed; analysis 90 passed/1 skipped; repository verify 1112 passed/2 skipped; controlled E2E repeat-5 5/5; authenticated live remains not_run.
Stabilization S2A fix round 5/5 complete in this commit: exact artifact-to-normalized-profile/note binding for every field plus owner/order/count/raw digest; validation-only restart certification and physical fact freeze; initial allocation commit acknowledgement classified committed/rolled_back/unknown with no second journal or SQLAlchemy leak; and reclaimable allocating/no-stage recovery including the recovery-before-stage race. Dedicated round-5 59 passed three times; focused 494 passed/1 skipped; analysis 90 passed/1 skipped; repository verify 1171 passed/2 skipped; frontend 47 passed, build and controlled E2E 1/1; authenticated live remains not_run.
Stabilization S2B pending: append-only/versioned note evidence and post-model pre-commit trust revalidation.
