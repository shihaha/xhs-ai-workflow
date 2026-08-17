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
