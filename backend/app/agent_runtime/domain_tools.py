"""Project-owned Agent Runtime wrappers around existing domain services.

Stage 2 intentionally wraps working business logic rather than reimplementing it.
"""

from __future__ import annotations

from backend.app.agent_runtime.tools import (
    ToolDomainFailureError,
    ToolNeedsHumanError,
    ToolSpec,
)
from backend.app.agent_runtime.types import ToolExecutionResult
from backend.app.features.analysis.schemas import AnalysisCreate
from backend.app.features.analysis.service import AnalysisService


ANALYSIS_RUN_TOOL_NAME = "analysis.run_grounded"


def build_analysis_run_tool(service: AnalysisService) -> ToolSpec:
    """Wrap the existing evidence-grounded AnalysisService without changing its rules."""

    def run(payload: AnalysisCreate) -> ToolExecutionResult:
        result = service.create(payload)
        tool_result = ToolExecutionResult(
            output=result.model_dump(mode="json"),
            evidence_refs=list(result.evidence_ids),
            summary=f"analysis:{result.status}:{result.id}",
        )
        if result.status == "needs_human":
            raise ToolNeedsHumanError(
                category=result.error_category or "analysis_needs_human",
                detail=result.error_detail or "Grounded analysis requires human intervention.",
                result=tool_result,
            )
        if result.status == "failed":
            raise ToolDomainFailureError(
                category=result.error_category or "analysis_failed",
                detail=result.error_detail or "Grounded analysis failed.",
                result=tool_result,
            )
        return tool_result

    return ToolSpec(
        name=ANALYSIS_RUN_TOOL_NAME,
        description=(
            "Run the existing evidence-grounded analysis service for one explicitly scoped "
            "analysis request. This wrapper does not weaken evidence, opportunity, or phase gates."
        ),
        input_model=AnalysisCreate,
        handler=run,
        read_only=False,
        destructive=False,
        external_side_effect=False,
        requires_approval=True,
        timeout_seconds=180.0,
        idempotent=False,
        retry_limit=0,
    )
