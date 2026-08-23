"""Read-only HTTP surface for durable Job-bound Agent execution state."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from backend.app.agent_runtime.workbench_read import (
    AgentWorkbenchReadError,
    AgentWorkbenchReader,
)
from backend.app.schemas.agent_runtime import AgentJobRuntimeRead, AgentRunDetailRead


router = APIRouter(prefix="/api/v1/agent-runtime", tags=["agent-runtime"])


def _reader(request: Request) -> AgentWorkbenchReader:
    reader: AgentWorkbenchReader | None = getattr(
        request.app.state, "agent_workbench_reader", None
    )
    if reader is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return reader


@router.get("/jobs/{job_id}", response_model=AgentJobRuntimeRead)
def get_agent_job_runtime(job_id: str, request: Request) -> AgentJobRuntimeRead:
    try:
        return AgentJobRuntimeRead.model_validate(_reader(request).job_view(job_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AgentWorkbenchReadError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/runs/{run_id}", response_model=AgentRunDetailRead)
def get_agent_run_detail(run_id: str, request: Request) -> AgentRunDetailRead:
    try:
        return AgentRunDetailRead.model_validate(_reader(request).run_view(run_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AgentWorkbenchReadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
