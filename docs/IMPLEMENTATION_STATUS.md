# Implementation status

Status date: 2026-08-19

## Software scope

Tasks 1–9 have implemented the local FastAPI/React/SQLite workbench, durable
jobs/evidence, normalized adapters, Qianfan orchestration, Android N/N workflow,
grounded Bailian analysis, reviewed content production, quarantined cleanup, and
the complete operator UI. Task 10 supplies release/recovery/security checks and
Windows runbooks.

The account-note collection extension now adds the production read-only
`xhs-cli` adapter path, reserved account/search jobs, exact profile-plus-note N/N
persistence, hash-bound evidence, account-note analysis grounding, and truthful
Account/Radar operator states. Account notes can ground analysis claims; they do
not replace the existing exact shop N/N gate for opportunity creation.

Account/search polling now allows 60 checks by default. Reaching that bound
releases the page-level single-flight lock without inventing a terminal job
state: the prior job stays visible for audit and can be resumed by its exact
returned ID or superseded by a newly submitted job. Account-note evidence is
selectable and labelled trusted only when the API returns its trust eligibility
flag as true; false entries are disabled and require human verification.

The Bailian media extension adds separate trusted vision and image-generation
configuration, durable reserved media runs, managed generated `output_image`
files, sealed advisory visual assessments, worker/API lifecycle and Content
Studio controls. Requests expose only current revision, image-plan entry and
managed material IDs; operators cannot enter provider endpoints, models, URLs
or filesystem paths. Visual advice cannot approve content or satisfy a human
per-image check.

The controlled Playwright flow begins with a fresh temporary SQLite database and
runs ranking collection through an available ZIP using deterministic test-only
Qianfan/device/model adapters and the production XHS adapter fed pinned-CLI JSON
shapes. It creates a unique ranked account on every
run, starts account-note collection from the UI through the production Task 3
job/artifact/database service, selects the newly persisted `account-note:*`
identity for analysis, and does not pre-seed account notes or substitute a
manual-import account. A fresh production database inserts no demo business
records. The cleanup API is read-only; reserved worker jobs reject public
result/log/artifact forgery; paths are runtime-contained; credentials are
environment-only.

## Release hardening decisions

- `xhs-cli` reads require an externally prepared cookie file in an app-owned
  state directory. The child gets an allowlisted environment whose profile and
  cache paths all resolve under that directory; missing state is
  `needs_human/login_required` and no CLI process starts.
- The XHS child uses fixed argv with `shell=False`. Stdout and stderr are bounded
  while the process runs; timeout or overflow terminates/reaps the child and
  yields only a sanitized category. Real top-level list, `userPageData`,
  `userInfo`, nested `noteCard`, profile-stat and xsec shapes are covered.
- Job evidence is fail-safe retained on cancellation, rollback and uncertain
  commit acknowledgement; no job failure path permanently unlinks the only copy.
- Ambiguous source URL literals (empty query/fragment markers and doubled
  leading path slash) are rejected instead of normalized silently.
- Identical analysis digests are explicitly allowed as separately audited
  intentional reruns; no cross-analysis opportunity merge occurs.
- Python requirement is 3.12+; Node minimum is 20.19+ and the verified version
  is pinned to 24.18.0.

## Recovery/security coverage inventory

| Scenario/boundary | Automated evidence |
|---|---|
| Lost login/cookie, captcha, layout timeout | `backend/tests/radar/test_rank_ingestion.py`, `backend/tests/shops/test_shop_collection.py` |
| Device disconnect/cancellation/N-N mismatch | `backend/tests/shops/` |
| Model 429, timeout, network exhaustion, secret redaction | `backend/tests/analysis/test_model_failures.py` |
| Process/job/cleanup restart | `backend/tests/integration/test_recovery_matrix.py`, cleanup worker/service suites |
| Job evidence commit ambiguity | `backend/tests/test_release_hardening.py` |
| Traversal/containment and Windows-equivalent artifact paths | jobs/content hardening and quarantine suites |
| Reserved worker APIs and read-only cleanup API | `backend/tests/test_jobs_api.py`, `backend/tests/content/test_cleanup_api.py` |
| Account/note collection states, polling release/resume and audit history | `frontend/src/pages/AccountPage.test.tsx`, `frontend/src/pages/RadarPage.test.tsx` |
| Account-note trust through fresh empty runtime and ZIP | `backend/tests/analysis/test_account_note_grounding.py`, `frontend/e2e/empty-to-package.spec.ts` |
| Generated PNG through API/worker/DB/file/UI, advisory vision and human-gated ZIP | media backend suites, `frontend/src/pages/ContentStudioPage.test.tsx`, `frontend/e2e/empty-to-package.spec.ts` |
| Guarded Bailian image/vision live gate | `backend/tests/integration/test_bailian_media_live.py` (explicit isolated opt-in passed on 2026-08-19 with real `wan2.6-t2i` and `qwen-vl-max`; default without opt-in remains exact `not_run`) |
| Guarded local XHS CLI contract | `backend/tests/xhs/test_live_cli_contract.py` (isolated externally prepared state, real `status` text shape, JSON `whoami` identity, exact read-only commands; bounded live gate passed on 2026-08-19 for current profile + 3 notes + empty search 0/0; default without opt-in remains `not_run`) |
| Fresh empty runtime through ZIP | recovery-matrix empty-state test and `frontend/e2e/empty-to-package.spec.ts` |

## Not verified live

Authenticated Qianfan, a non-empty live XHS keyword search and ranked-account UI
workflow, a real Android phone, Bailian text, and the seven-day run remain
`not_run`. The bounded authenticated local `xhs-cli` gate passed for the current
profile, 3 notes and an exact empty search on 2026-08-19.
Bailian image generation and visual assessment passed their bounded real-model
gate on 2026-08-19. See `docs/UAT_CHECKLIST.md`. Therefore the honest release label is
**software implemented / awaiting real UAT**.
