# Xiaohongshu Intelligence Workbench

Windows-local workbench for evidence-backed Xiaohongshu research and analysis.

## Local development

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

The current screens are `/status` for API-reported prerequisites and `/jobs` for persisted job, log, and evidence facts. A fresh runtime has no sample jobs.
