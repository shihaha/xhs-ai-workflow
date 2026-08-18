# Implementation status

Status date: 2026-08-18

## Software scope

Tasks 1–9 have implemented the local FastAPI/React/SQLite workbench, durable
jobs/evidence, normalized adapters, Qianfan orchestration, Android N/N workflow,
grounded Bailian analysis, reviewed content production, quarantined cleanup, and
the complete operator UI. Task 10 supplies release/recovery/security checks and
Windows runbooks.

The controlled Playwright flow begins with a fresh temporary SQLite database and
runs ranking collection through an available ZIP using deterministic test-only
Qianfan/device/model adapters. A fresh production database inserts no demo
business records. The cleanup API is read-only; reserved worker jobs reject
public result/log/artifact forgery; paths are runtime-contained; credentials are
environment-only.

## Release hardening decisions

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
| Fresh empty runtime through ZIP | recovery-matrix empty-state test and `frontend/e2e/empty-to-package.spec.ts` |

## Not verified live

Authenticated Qianfan, a real Android phone, Bailian using a user key, and the
seven-day run remain `not_run`. See `docs/UAT_CHECKLIST.md`. Therefore the honest
release label is **software implemented / awaiting real UAT**.
