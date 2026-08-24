"""Permission-gated Agent wrappers for existing durable physical workers.

The Agent never drives Android/XHS directly.  A physical Tool may only validate
an exact durable target and enqueue work into an already-owned domain service.
The domain service/worker remains lifecycle authority for the external action.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from backend.app.agent_runtime.tools import ToolExecutionContext, ToolNeedsHumanError, ToolSpec
from backend.app.agent_runtime.types import ToolExecutionResult
from backend.app.features.radar.service import (
    CandidateFunnelBlocked,
    CandidateFunnelExhausted,
    RadarService,
)
from backend.app.features.shops.service import ShopCollectionCreate, ShopCollectionService


SHOP_PREFLIGHT_TOOL_NAME = "shop.preflight"


class ShopPreflightInput(BaseModel):
    """Exact target approved by the operator before any Android work is queued."""

    model_config = ConfigDict(extra="forbid")

    source_date: date
    account_user_id: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class ShopPreflightTargetChanged(ValueError):
    """The persisted target is no longer the next authoritative funnel candidate."""


def _resolve_exact_candidate(radar: RadarService, payload: ShopPreflightInput):
    try:
        candidate = radar.next_preflight_candidate(source_date=payload.source_date)
    except (CandidateFunnelBlocked, CandidateFunnelExhausted) as error:
        raise ShopPreflightTargetChanged(str(error)) from error
    if candidate.user_id != payload.account_user_id:
        raise ShopPreflightTargetChanged(
            "The approved account is no longer the next authoritative preflight candidate."
        )
    return candidate


def build_shop_preflight_tool(
    *,
    radar_service: RadarService,
    shop_service: ShopCollectionService,
) -> ToolSpec:
    """Queue one existing Android preflight without transferring worker ownership."""

    def available() -> bool:
        try:
            return shop_service.device_adapter.health().status == "available"
        except Exception:
            return False

    def pre_approval_validate(payload: BaseModel) -> None:
        exact = ShopPreflightInput.model_validate(payload.model_dump(mode="json"))
        _resolve_exact_candidate(radar_service, exact)

    def run(
        payload: ShopPreflightInput,
        execution: ToolExecutionContext,
    ) -> ToolExecutionResult:
        try:
            candidate = _resolve_exact_candidate(radar_service, payload)
        except ShopPreflightTargetChanged as error:
            raise ToolNeedsHumanError(
                category="shop_preflight_target_changed",
                detail=str(error),
            ) from error

        queued = shop_service.enqueue(
            ShopCollectionCreate(
                account_user_id=candidate.user_id,
                account_name=candidate.account_name,
                collection_mode="preflight",
            ),
            agent_origin=(execution.run_id, execution.tool_call_id),
        )
        result = ToolExecutionResult(
            output={
                "job_id": queued.job_id,
                "status": queued.status,
                "source_date": payload.source_date.isoformat(),
                "account_user_id": candidate.user_id,
                "candidate_position": candidate.candidate_position,
                "collection_mode": "preflight",
            },
            summary=f"shop_preflight_queued:{queued.job_id}",
        )
        # Queuing a physical worker is not completion.  Stop the Agent loop at a
        # durable wait boundary so no provider/model call can run ahead of the
        # Android Job.  A separate reconciler consumes the child Job's durable
        # terminal result and either creates a ChatGPT handoff or continues the
        # deterministic domain path without replaying the phone action.
        raise ToolNeedsHumanError(
            category="physical_job_pending",
            detail=(
                "Durable Android shop preflight was queued; waiting for its persisted "
                "Job result before any further Agent reasoning."
            ),
            result=result,
        )

    return ToolSpec(
        name=SHOP_PREFLIGHT_TOOL_NAME,
        description=(
            "Queue the existing durable Android/XHS shop preflight worker for the exact "
            "next ranked candidate. The account and source date must remain authoritative "
            "at approval and execution time. Queuing is not collection completion."
        ),
        input_model=ShopPreflightInput,
        handler=run,
        read_only=False,
        destructive=False,
        external_side_effect=True,
        concurrency_safe=False,
        requires_approval=True,
        available=available,
        pre_approval_validate=pre_approval_validate,
        timeout_seconds=15.0,
        idempotent=False,
        retry_limit=0,
    )
