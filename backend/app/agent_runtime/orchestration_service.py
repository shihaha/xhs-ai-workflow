"""Application service for starting evidence-grounded Agent orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.agent_runtime.initial_execution import (
    AgentInitialExecutor,
    InitialAgentLaunchError,
)
from backend.app.agent_runtime.types import RunBudget
from backend.app.features.analysis.service import AnalysisService


INITIAL_ORCHESTRATION_MAX_EVIDENCE = 20
INITIAL_ORCHESTRATION_BUDGET = RunBudget(
    max_steps=12,
    max_model_calls=6,
    max_input_tokens=60_000,
    max_output_tokens=10_000,
    max_wall_time_seconds=180.0,
)


class AgentOrchestrationStartError(RuntimeError):
    """A new grounded orchestration Job cannot be admitted safely."""


@dataclass(frozen=True, slots=True)
class AgentOrchestrationStartResult:
    job_id: str
    run_id: str
    evidence_count: int


class AgentOrchestrationService:
    """Validate durable evidence scope before creating an initial Agent Job."""

    def __init__(
        self,
        *,
        analysis_service: AnalysisService,
        initial_executor: AgentInitialExecutor,
    ) -> None:
        self.analysis_service = analysis_service
        self.initial_executor = initial_executor

    @property
    def available(self) -> bool:
        return self.initial_executor.accepting

    def start_grounded_analysis(
        self,
        *,
        goal: str,
        evidence_ids: list[str],
    ) -> AgentOrchestrationStartResult:
        if not self.available:
            raise AgentOrchestrationStartError(
                "Automatic initial Agent execution is not configured."
            )
        if not evidence_ids or len(evidence_ids) > INITIAL_ORCHESTRATION_MAX_EVIDENCE:
            raise AgentOrchestrationStartError(
                f"Initial orchestration requires 1-{INITIAL_ORCHESTRATION_MAX_EVIDENCE} evidence ids."
            )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise AgentOrchestrationStartError("Evidence ids must be unique.")

        available = {item.evidence_id: item for item in self.analysis_service.list_evidence()}
        missing = [evidence_id for evidence_id in evidence_ids if evidence_id not in available]
        if missing:
            raise AgentOrchestrationStartError(
                "Unknown or currently unavailable durable evidence: " + ", ".join(missing)
            )

        summaries = [
            {
                "evidence_id": evidence_id,
                "kind": available[evidence_id].kind,
                "account_user_id": available[evidence_id].account_user_id,
                "eligible_for_opportunity": available[evidence_id].eligible_for_opportunity,
            }
            for evidence_id in evidence_ids
        ]
        try:
            launch = self.initial_executor.launch(
                goal=goal,
                evidence_refs=evidence_ids,
                evidence_summaries=summaries,
                budget=INITIAL_ORCHESTRATION_BUDGET,
            )
        except InitialAgentLaunchError as error:
            raise AgentOrchestrationStartError(str(error)) from error
        return AgentOrchestrationStartResult(
            job_id=launch.job_id,
            run_id=launch.run_id,
            evidence_count=len(evidence_ids),
        )
