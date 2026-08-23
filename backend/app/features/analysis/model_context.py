"""Deterministic model-only evidence projection for the Stage 3 compaction spike.

This module never decides whether evidence is trusted. ``AnalysisService`` resolves,
verifies, fingerprints, digests and later grounds against the full persisted facts.
The adapter below only removes audit-only bulk immediately before a structured model
request is sent downstream.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from backend.app.adapters.contracts import ModelResult, StructuredModelRequest


COMPACT_CONTEXT_VERSION = "grounded-evidence-model-view-v1"
COMPACT_PROMPT_VERSION_SUFFIX = "-compact-context-v1"

_SHOP_RESULT_KEYS = (
    "job_id",
    "status",
    "detail",
    "selector_profile_version",
    "collection_mode",
    "expected_count",
    "discovered_count",
    "collected_count",
    "succeeded_count",
    "available_count_observed",
    "complete",
    "sample_complete",
    "shop_complete",
    "natural_end_reached",
    "scope_classification",
    "scope_reason",
)
_VERIFICATION_KEYS = (
    "expected_count",
    "discovered_count",
    "succeeded_count",
    "missing_count",
    "overflow_count",
    "complete",
)


def build_model_evidence(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a smaller reasoning view without mutating the trusted facts.

    Evidence identity and account ownership are mandatory in every projected row.
    Unknown evidence kinds retain their existing small top-level facts rather than
    silently disappearing, but raw evidence blobs are never copied into the model view.
    """

    projected: list[dict[str, Any]] = []
    for fact in facts:
        if not isinstance(fact, dict):
            raise ValueError("analysis evidence fact must be an object")
        evidence_id = fact.get("evidence_id")
        kind = fact.get("kind")
        account_user_id = fact.get("account_user_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError("analysis evidence fact is missing evidence_id")
        if not isinstance(kind, str) or not kind:
            raise ValueError("analysis evidence fact is missing kind")
        if not isinstance(account_user_id, str) or not account_user_id:
            raise ValueError("analysis evidence fact is missing account ownership")

        if kind == "account_note":
            row = {
                "evidence_id": evidence_id,
                "kind": kind,
                "account_user_id": account_user_id,
                "facts": deepcopy(fact.get("facts", {})),
            }
        elif kind == "rank_item":
            row = {
                "evidence_id": evidence_id,
                "kind": kind,
                "account_user_id": account_user_id,
                "user_id": fact.get("user_id"),
                "source_url": fact.get("source_url"),
                "facts": deepcopy(fact.get("facts", {})),
            }
        elif kind == "shop_collection_result":
            row = {
                "evidence_id": evidence_id,
                "kind": kind,
                "account_user_id": account_user_id,
                "job_id": fact.get("job_id"),
                "job_state": fact.get("job_state"),
                "trusted_shop_result": _project_shop_result(
                    fact.get("trusted_shop_result")
                ),
            }
        else:
            # Existing non-shop artifact facts contain only identity/job metadata plus
            # a null trusted_shop_result. Keep useful normalized fields, never opaque
            # raw evidence or artifact metadata/path/hash material.
            row = {
                "evidence_id": evidence_id,
                "kind": kind,
                "account_user_id": account_user_id,
            }
            for key in ("job_id", "job_state", "source_url", "facts"):
                if key in fact:
                    row[key] = deepcopy(fact[key])
        projected.append(row)
    return projected


def build_compact_user_prompt(
    request: StructuredModelRequest,
    schema: type[BaseModel],
) -> StructuredModelRequest:
    """Build the compact request while proving that the duplicate schema is identical.

    Bailian already injects ``schema.model_json_schema()`` into its system message. The
    legacy analysis user prompt also embeds the same schema as ``required_schema``.
    We only remove that second copy after exact structural equality is established.
    """

    try:
        payload = json.loads(request.user_prompt)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("analysis model user prompt must be a JSON object") from error
    if not isinstance(payload, dict):
        raise ValueError("analysis model user prompt must be a JSON object")
    allowed = payload.get("allowed_evidence")
    if not isinstance(allowed, list):
        raise ValueError("analysis model prompt is missing allowed_evidence")
    required_schema = payload.get("required_schema")
    if required_schema != schema.model_json_schema():
        raise ValueError("analysis prompt schema does not match structured output schema")

    compact_payload = {
        "context_version": COMPACT_CONTEXT_VERSION,
        "analysis_type": payload.get("analysis_type"),
        "account_user_id": payload.get("account_user_id"),
        "account_user_ids": payload.get("account_user_ids"),
        "allowed_evidence": build_model_evidence(allowed),
    }
    return StructuredModelRequest(
        system_prompt=request.system_prompt,
        user_prompt=json.dumps(
            compact_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        prompt_version=request.prompt_version + COMPACT_PROMPT_VERSION_SUFFIX,
        evidence_ids=list(request.evidence_ids),
    )


class CompactingModelAdapter:
    """Model-adapter decorator used only by the isolated Stage 3 experiment."""

    def __init__(self, downstream: Any) -> None:
        self.downstream = downstream

    @property
    def provider(self) -> str:
        return str(self.downstream.provider)

    @property
    def model(self) -> str:
        return str(self.downstream.model)

    @property
    def configured(self) -> bool:
        return bool(self.downstream.configured)

    def generate_structured(
        self,
        request: StructuredModelRequest,
        schema: type[BaseModel],
    ) -> ModelResult:
        compact = build_compact_user_prompt(request, schema)
        return self.downstream.generate_structured(compact, schema)


def prompt_size(value: str) -> dict[str, int]:
    """Structural size metrics only; these are not provider-token estimates."""

    return {
        "characters": len(value),
        "utf8_bytes": len(value.encode("utf-8")),
    }


def _project_shop_result(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("trusted shop result must be an object or null")
    result = {key: deepcopy(value.get(key)) for key in _SHOP_RESULT_KEYS if key in value}

    items = value.get("items", [])
    if not isinstance(items, list):
        raise ValueError("trusted shop result items must be a list")
    compact_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("trusted shop result item must be an object")
        compact_items.append(
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "source_url": item.get("source_url"),
                # Normalized product data remains intact. Only opaque raw capture is
                # removed; title/price/category/description-like normalized fields are
                # therefore still available to the reasoning model.
                "data": deepcopy(item.get("data", {})),
            }
        )
    result["items"] = compact_items

    verification = value.get("verification")
    if verification is None:
        result["verification"] = None
    elif isinstance(verification, dict):
        result["verification"] = {
            key: deepcopy(verification.get(key))
            for key in _VERIFICATION_KEYS
            if key in verification
        }
    else:
        raise ValueError("trusted shop verification must be an object or null")
    return result
