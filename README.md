# Xiaohongshu Intelligence Workbench

Windows-local workbench for evidence-backed Xiaohongshu research and analysis.

## Local development

Prerequisites: Python 3.12+ and Node 20.19+. The repository's verified Node
toolchain is pinned in `.node-version` (24.18.0).

```powershell
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m uvicorn backend.app.main:app --reload
```

Check the live local prerequisites at `http://127.0.0.1:8000/api/v1/health`.
The default runtime directory is `D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench`, outside the repository.

## Local dashboard

In a second terminal, start the React shell and open the printed local URL. It proxies `/api` requests to the FastAPI server on port 8000.

```powershell
Set-Location frontend
npm install
npm run dev
```

The operator flow is available at `/radar`, `/accounts/<user-id>`, `/opportunities`, and `/content`. System prerequisites remain at `/status`, while `/jobs` shows persisted job, log, progress, and evidence facts. A fresh runtime has no sample business records. The active Phase A workflow stops after cross-account demand validation and human opportunity review; product/content execution is not part of Phase A.

The ranking page reads stored snapshots. The backend can reserve the fixed Qianfan four-board by two-dimension pass with `POST /api/v1/radar/qianfan-collections` and a body such as `{"expected_count_per_scope": 20}`. The endpoint creates eight evidence-backed scope jobs; it does not accept URLs, selectors, scripts, profile paths, dates, or profile versions. The checked-in live selector profile remains explicitly unsupported until an authenticated current-layout probe verifies it, so current live attempts stop as `needs_human` rather than inventing snapshots. Configure the browser executable and an existing persistent login profile only through `XHS_BROWSER_EXECUTABLE` and `XHS_QIANFAN_BROWSER_PROFILE_DIR`.

Shop verification can be queued from an account using a runtime-relative evidence directory. Content export creates a local pending-publication ZIP only and never publishes to Xiaohongshu.

One account is an observation signal, not a market opportunity. Cross-account analysis requires at least two distinct accounts with trusted notes and complete shop evidence. The service computes the evidence level, persists valid clusters as `pending_review`, and requires an explicit human `approved` decision before any product record can be created.

Frontend verification:

```powershell
Set-Location frontend
npm test -- --run
npm run build
npm run test:e2e
```

The Playwright test starts a test-only FastAPI application with a fresh temporary SQLite database and controlled Qianfan/device/model adapters. It is not a live Qianfan, Android, or Bailian verification.

For normal local operation and the complete release checks, use
`scripts/run-local.ps1` and `scripts/verify.ps1`. Operational recovery and the
remaining live gates are documented in `docs/RUNBOOK.md` and
`docs/UAT_CHECKLIST.md`.
