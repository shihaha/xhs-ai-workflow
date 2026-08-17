"""Normalized, evidence-linked contracts for every external adapter."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator


class CollectionRequest(BaseModel):
    """A third-party-neutral request for a supported collection capability."""

    capability: str = Field(min_length=1, max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_count: int | None = Field(default=None, ge=0)


class CollectionItem(BaseModel):
    """One normalized item with its source location and preserved raw evidence."""

    id: str = Field(min_length=1, max_length=500)
    kind: str = Field(min_length=1, max_length=100)
    source_url: AnyHttpUrl
    raw_evidence: dict[str, Any] = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class MissingCollectionItem(BaseModel):
    """An expected item that could not be collected, with an auditable cause."""

    reference: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    raw_evidence: dict[str, Any] = Field(min_length=1)


class RejectedCollectionItem(BaseModel):
    """An observed source row rejected during normalization, with its original evidence."""

    reference: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    raw_evidence: dict[str, Any] = Field(min_length=1)


class CollectionResult(BaseModel):
    """Accounted collection output; completion is invalid unless the N/N facts match."""

    status: Literal["succeeded", "partial", "needs_human", "failed"] = "partial"
    detail: str | None = Field(default=None, max_length=1000)
    evidence_artifacts: list[str] = Field(default_factory=list)
    items: list[CollectionItem] = Field(default_factory=list)
    rejected_items: list[RejectedCollectionItem] = Field(default_factory=list)
    expected_count_known: bool
    expected_count: int | None = Field(default=None, ge=0)
    succeeded_count: int = Field(ge=0)
    observed_count: int | None = Field(default=None, ge=0)
    raw_observation_count: int | None = Field(default=None, ge=0)
    duplicate_observation_count: int = Field(default=0, ge=0)
    missing_items: list[MissingCollectionItem] = Field(default_factory=list)
    overflow_count: int = Field(ge=0)
    complete: bool = False

    @model_validator(mode="after")
    def account_for_every_expected_item(self) -> "CollectionResult":
        item_ids = [item.id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Collection result items must have distinct stable ids.")
        rejected_references = [item.reference for item in self.rejected_items]
        if len(rejected_references) != len(set(rejected_references)):
            raise ValueError("Rejected observations must have distinct source references.")
        if self.succeeded_count != len(self.items):
            raise ValueError("succeeded_count must equal the number of stored items.")
        accounted_observed = self.succeeded_count + len(self.rejected_items)
        if self.observed_count is None:
            self.observed_count = accounted_observed
        elif self.observed_count != accounted_observed:
            raise ValueError(
                "observed_count must equal accepted items plus rejected observations."
            )
        if self.raw_observation_count is None:
            self.raw_observation_count = accounted_observed
        elif self.raw_observation_count != accounted_observed:
            raise ValueError(
                "raw_observation_count must equal accepted items plus rejected observations."
            )
        inferred_duplicate_observations = sum(
            item.reason == "duplicate_source_url" for item in self.rejected_items
        )
        if "duplicate_observation_count" not in self.model_fields_set:
            self.duplicate_observation_count = inferred_duplicate_observations
        elif self.duplicate_observation_count != inferred_duplicate_observations:
            raise ValueError(
                "duplicate_observation_count must equal duplicate_source_url rejections."
            )
        identity_observed = accounted_observed - self.duplicate_observation_count
        non_duplicate_rejections = (
            len(self.rejected_items) - self.duplicate_observation_count
        )
        if non_duplicate_rejections:
            if self.status not in {"needs_human", "failed"} or self.complete:
                raise ValueError(
                    "non-duplicate rejected observations require needs_human or failed incomplete status."
                )
        if not self.expected_count_known:
            if self.expected_count is not None:
                raise ValueError("unknown expected counts must not publish an N total.")
            if self.complete:
                raise ValueError("unknown expected counts cannot be complete.")
            if self.status == "succeeded":
                raise ValueError("unknown expected counts cannot be succeeded.")
            if self.missing_items:
                raise ValueError("unknown expected counts cannot fabricate missing items.")
            if self.overflow_count:
                raise ValueError("unknown expected counts cannot claim overflow.")
            if self.items and not self.rejected_items and self.status not in {
                "partial",
                "needs_human",
            }:
                raise ValueError(
                    "unknown-total observations require partial or needs_human status."
                )
            return self

        if self.expected_count is None:
            raise ValueError("known expected counts require an expected_count.")
        deficit = max(self.expected_count - identity_observed, 0)
        overflow = max(identity_observed - self.expected_count, 0)
        if len(self.missing_items) != deficit:
            raise ValueError(
                "missing_items must exactly account for the known expected deficit."
            )
        if self.overflow_count != overflow:
            raise ValueError("overflow_count must exactly account for excess observed items.")
        if overflow:
            if self.status != "failed" or self.complete:
                raise ValueError("overflow results must be failed and incomplete.")
            return self
        if deficit:
            if self.status == "succeeded" or self.complete:
                raise ValueError("known deficits cannot be succeeded or complete.")
            return self
        if non_duplicate_rejections:
            return self
        if self.expected_count > 0:
            if self.status != "succeeded" or not self.complete:
                raise ValueError(
                    "positive exact known results must be succeeded and complete."
                )
            return self
        if self.status == "partial":
            raise ValueError("partial results require a real known deficit.")
        if self.status == "succeeded" and not self.complete:
            raise ValueError("succeeded zero-of-zero results must be complete.")
        if self.status != "succeeded" and self.complete:
            raise ValueError("only succeeded results can be complete.")
        return self


class DeviceHealth(BaseModel):
    """Observed device availability, never an inferred healthy state."""

    status: Literal["available", "unavailable", "needs_human"]
    device_id: str | None = Field(default=None, max_length=500)
    detail: str = Field(min_length=1, max_length=1000)
    raw_evidence: dict[str, Any] = Field(min_length=1)


class ModelResult(BaseModel):
    """A structured-model result bound to the raw provider evidence that produced it."""

    model: str = Field(min_length=1, max_length=300)
    output: dict[str, Any]
    raw_evidence: dict[str, Any] = Field(min_length=1)
    usage: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = Field(default=None, ge=0)


class CollectorAdapter(Protocol):
    """Protocol implemented by collection sources without leaking their native shapes."""

    def collect_rankings(self, request: CollectionRequest) -> CollectionResult: ...

    def search_notes(self, request: CollectionRequest) -> CollectionResult: ...

    def fetch_account(self, request: CollectionRequest) -> CollectionResult: ...

    def fetch_products(self, request: CollectionRequest) -> CollectionResult: ...


class DeviceAdapter(Protocol):
    """Protocol for an Android device adapter."""

    def health(self) -> DeviceHealth: ...

    def collect_shop(self, request: CollectionRequest) -> CollectionResult: ...


class ModelAdapter(Protocol):
    """Protocol for a model provider adapter."""

    def generate_structured(
        self, request: CollectionRequest, schema: type[BaseModel]
    ) -> ModelResult: ...
