"""Read-only HTTP surface for durable Job-bound Agent execution state."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from backend.app.agent_runtime.workbench_read import (
    AgentWorkbenchReadError,
    AgentWorkbenchReader,
)
from backend.app.schemas.agent_runtime import (
    AgentEvidenceRefsRead,
    AgentJobRuntimeRead,
    AgentJobSummaryRead,
    AgentRunDetailRead,
    AgentRunListItemRead,
    HumanActionWorkbenchRead,
    JobArtifactSummaryRead,
)


router = APIRouter(prefix="/api/v1/agent-runtime", tags=["agent-runtime"])


def _reader(request: Request) -> AgentWorkbenchReader:
    reader: AgentWorkbenchReader | None = getattr(
        request.app.state, "agent_workbench_reader", None
    )
    if reader is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return reader


def _job_view(job_id: str, request: Request) -> dict:
    try:
        return _reader(request).job_view(job_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AgentWorkbenchReadError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/jobs", response_model=list[AgentJobSummaryRead])
def list_agent_jobs(request: Request) -> list[AgentJobSummaryRead]:
    try:
        return [
            AgentJobSummaryRead.model_validate(item)
            for item in _reader(request).list_jobs()
        ]
    except AgentWorkbenchReadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/jobs/{job_id}", response_model=AgentJobRuntimeRead)
def get_agent_job_runtime(job_id: str, request: Request) -> AgentJobRuntimeRead:
    return AgentJobRuntimeRead.model_validate(_job_view(job_id, request))


@router.get(
    "/jobs/{job_id}/artifacts", response_model=list[JobArtifactSummaryRead]
)
def get_agent_job_artifacts(
    job_id: str, request: Request
) -> list[JobArtifactSummaryRead]:
    _job_view(job_id, request)
    return [
        JobArtifactSummaryRead.model_validate(item)
        for item in _reader(request).job_artifacts(job_id)
    ]


@router.get("/jobs/{job_id}/evidence", response_model=AgentEvidenceRefsRead)
def get_agent_job_evidence(job_id: str, request: Request) -> AgentEvidenceRefsRead:
    _job_view(job_id, request)
    return AgentEvidenceRefsRead.model_validate(_reader(request).job_evidence(job_id))


@router.get("/runs", response_model=list[AgentRunListItemRead])
def list_agent_runs(request: Request) -> list[AgentRunListItemRead]:
    try:
        return [
            AgentRunListItemRead.model_validate(item)
            for item in _reader(request).list_runs()
        ]
    except AgentWorkbenchReadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/runs/{run_id}", response_model=AgentRunDetailRead)
def get_agent_run_detail(run_id: str, request: Request) -> AgentRunDetailRead:
    try:
        return AgentRunDetailRead.model_validate(_reader(request).run_view(run_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AgentWorkbenchReadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/human-actions", response_model=list[HumanActionWorkbenchRead])
def list_human_actions(
    request: Request,
    status: Literal["pending", "approved", "denied"] | None = None,
) -> list[HumanActionWorkbenchRead]:
    return [
        HumanActionWorkbenchRead.model_validate(item)
        for item in _reader(request).list_human_actions(status=status)
    ]
