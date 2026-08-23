"""Durable Agent workbench read surface plus narrow operator commands."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from backend.app.agent_runtime.workbench_actions import (
    AgentWorkbenchActionError,
    AgentWorkbenchActionService,
)
from backend.app.agent_runtime.workbench_read import (
    AgentWorkbenchReadError,
    AgentWorkbenchReader,
)
from backend.app.schemas.agent_runtime import (
    AgentActionCapabilitiesRead,
    AgentEvidenceRefsRead,
    AgentJobCancelRead,
    AgentJobRuntimeRead,
    AgentJobSummaryRead,
    AgentRunDetailRead,
    AgentRunListItemRead,
    ChatGPTHandoffTaskRead,
    HumanActionApprovalRead,
    HumanActionApproveRequest,
    HumanActionDecisionRead,
    HumanActionDenyRequest,
    HumanActionWorkbenchRead,
    JobArtifactSummaryRead,
)


router = APIRouter(prefix="/api/v1/agent-runtime", tags=["agent-runtime"])


def _actions(request: Request) -> AgentWorkbenchActionService:
    service: AgentWorkbenchActionService | None = getattr(
        request.app.state, "agent_workbench_actions", None
    )
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


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


@router.get("/operator-capabilities", response_model=AgentActionCapabilitiesRead)
def get_operator_capabilities(request: Request) -> AgentActionCapabilitiesRead:
    return AgentActionCapabilitiesRead.model_validate(_actions(request).capabilities())


@router.post("/jobs/{job_id}/cancel", response_model=AgentJobCancelRead)
def cancel_agent_job(job_id: str, request: Request) -> AgentJobCancelRead:
    try:
        result = _actions(request).cancel_job(job_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AgentWorkbenchActionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return AgentJobCancelRead(
        job_id=result.job_id,
        job_state="cancelled",
        cancelled_run_ids=list(result.cancelled_run_ids),
        resolved_human_action_ids=list(result.resolved_human_action_ids),
        already_cancelled=result.already_cancelled,
    )


@router.post(
    "/human-actions/{human_action_id}/approve",
    response_model=HumanActionApprovalRead,
)
def approve_agent_human_action(
    human_action_id: str,
    payload: HumanActionApproveRequest,
    request: Request,
) -> HumanActionApprovalRead:
    try:
        result = _actions(request).approve_permission_action(
            human_action_id,
            note=payload.note,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AgentWorkbenchActionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return HumanActionApprovalRead(
        human_action_id=result.human_action_id,
        job_id=result.job_id,
        source_run_id=result.source_run_id,
        continuation_run_id=result.continuation_run_id,
        human_action_status="approved",
        continuation_enqueued=True,
    )


@router.post("/human-actions/{human_action_id}/deny", response_model=HumanActionDecisionRead)
def deny_agent_human_action(
    human_action_id: str,
    payload: HumanActionDenyRequest,
    request: Request,
) -> HumanActionDecisionRead:
    try:
        result = _actions(request).deny_permission_action(
            human_action_id,
            note=payload.note,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AgentWorkbenchActionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return HumanActionDecisionRead(
        human_action_id=result.human_action_id,
        job_id=result.job_id,
        run_id=result.run_id,
        human_action_status="denied",
        job_state="failed",
        run_state="failed",
    )


@router.get("/human-actions", response_model=list[HumanActionWorkbenchRead])
def list_human_actions(
    request: Request,
    status: Literal["pending", "approved", "denied", "completed"] | None = None,
) -> list[HumanActionWorkbenchRead]:
    return [
        HumanActionWorkbenchRead.model_validate(item)
        for item in _reader(request).list_human_actions(status=status)
    ]


@router.get("/chatgpt-handoffs", response_model=list[ChatGPTHandoffTaskRead])
def list_chatgpt_handoffs(
    request: Request,
    status: Literal["pending", "accepted"] | None = None,
) -> list[ChatGPTHandoffTaskRead]:
    try:
        return [
            ChatGPTHandoffTaskRead.model_validate(item)
            for item in _reader(request).list_chatgpt_handoffs(status=status)
        ]
    except AgentWorkbenchReadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get(
    "/chatgpt-handoffs/{handoff_id}", response_model=ChatGPTHandoffTaskRead
)
def get_chatgpt_handoff(
    handoff_id: str, request: Request
) -> ChatGPTHandoffTaskRead:
    try:
        return ChatGPTHandoffTaskRead.model_validate(
            _reader(request).chatgpt_handoff_view(handoff_id)
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AgentWorkbenchReadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
