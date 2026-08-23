"""Production assembly for the bounded Job-bound Agent Runtime.

This module only composes already-owned model/service/tool boundaries. It does
not create new business workflow semantics and it never exposes physical
Android/XHS/browser workers as synchronous Agent tools.
"""

from __future__ import annotations

from backend.app.adapters.bailian import BailianModelAdapter
from backend.app.adapters.contracts import StructuredModelRequest
from backend.app.agent_runtime.context import RuntimeContext
from backend.app.agent_runtime.domain_tools import build_analysis_run_tool
from backend.app.agent_runtime.job_binding import build_job_read_tool
from backend.app.agent_runtime.job_bound_runtime import (
    ActiveRunJobAuthorityGuard,
    JobBoundAgentRuntime,
)
from backend.app.agent_runtime.job_continuation import (
    JobContinuationCoordinator,
    JobHistoryContextBuilder,
)
from backend.app.agent_runtime.job_wait_projection import JobHumanWaitProjector
from backend.app.agent_runtime.permissions import RuleBasedPermissionPolicy
from backend.app.agent_runtime.persistence import AgentRunStore
from backend.app.agent_runtime.tools import ToolRegistry
from backend.app.agent_runtime.types import ModelTurn, NextAction
from backend.app.db import Database
from backend.app.features.analysis.service import AnalysisService
from backend.app.services.jobs import JobService


_AGENT_PROMPT_VERSION = "job-bound-agent-v1"
_AGENT_INSTRUCTIONS = (
    "Choose exactly one next action for this bounded orchestration run. Use only "
    "tools present in the supplied context. Never invent tool success, evidence, "
    "or lifecycle state. Return finish only when the goal can be truthfully "
    "completed from durable results already present in context."
)


class AgentRuntimeNotConfigured(RuntimeError):
    """Automatic Agent model execution is unavailable for this local runtime."""


class BailianRuntimeDecisionModel:
    """Adapt the existing bounded Bailian adapter to the Agent ModelDriver contract."""

    def __init__(self, adapter: BailianModelAdapter) -> None:
        self.adapter = adapter

    def next_action(self, context: RuntimeContext) -> ModelTurn:
        evidence_ids = sorted(
            {
                evidence_id
                for step in context.recent_steps
                for evidence_id in step.evidence_refs
            }
        )
        if not evidence_ids:
            # Current production approval surface is grounded analysis, whose
            # committed Tool result carries real evidence refs. If that proof is
            # missing, do not create a provider request with fabricated scope.
            raise ValueError(
                "Automatic Agent decision requires at least one durable evidence ref in context."
            )
        result = self.adapter.generate_structured(
            StructuredModelRequest(
                system_prompt=_AGENT_INSTRUCTIONS,
                user_prompt=context.model_dump_json(),
                prompt_version=_AGENT_PROMPT_VERSION,
                evidence_ids=evidence_ids,
            ),
            NextAction,
        )
        action = NextAction.model_validate(result.output)
        usage = result.usage
        return ModelTurn(
            action=action,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model_name=result.model,
        )


def automatic_agent_configured(adapter: BailianModelAdapter) -> bool:
    return adapter.configured


def build_production_job_bound_runtime(
    *,
    database: Database,
    job_service: JobService,
    analysis_service: AnalysisService,
    bailian_adapter: BailianModelAdapter,
    continuations: JobContinuationCoordinator,
) -> JobBoundAgentRuntime:
    """Build one bounded runtime from the existing adapter and domain services."""

    if not bailian_adapter.configured:
        raise AgentRuntimeNotConfigured("Bailian automatic Agent execution is not configured.")

    tools = ToolRegistry()
    tools.register(build_job_read_tool(job_service))
    tools.register(build_analysis_run_tool(analysis_service))

    return JobBoundAgentRuntime(
        store=AgentRunStore(database),
        model=BailianRuntimeDecisionModel(bailian_adapter),
        tools=tools,
        permissions=RuleBasedPermissionPolicy(),
        authority_guard=ActiveRunJobAuthorityGuard(database),
        wait_projector=JobHumanWaitProjector(database),
        continuation_resolver=continuations,
        context_builder=JobHistoryContextBuilder(database),
    )
