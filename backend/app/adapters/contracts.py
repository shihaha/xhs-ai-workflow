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


class CollectionResult(BaseModel):
    """Accounted collection output; completion is invalid unless the N/N facts match."""

    status: Literal["succeeded", "partial", "needs_human", "failed"] = "succeeded"
    detail: str | None = Field(default=None, max_length=1000)
    evidence_artifacts: list[str] = Field(default_factory=list)
    items: list[CollectionItem] = Field(default_factory=list)
    expected_count: int | None = Field(default=None, ge=0)
    succeeded_count: int = Field(ge=0)
    missing_items: list[MissingCollectionItem] = Field(default_factory=list)
    complete: bool = False

    @model_validator(mode="after")
    def account_for_every_expected_item(self) -> "CollectionResult":
        item_ids = [item.id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Collection result items must have distinct stable ids.")
        if self.succeeded_count != len(self.items):
            raise ValueError("succeeded_count must equal the number of stored items.")
        if self.status == "needs_human" and self.complete:
            raise ValueError("needs_human collection results cannot be complete.")
        if self.expected_count is None:
            if self.complete:
                raise ValueError("complete collection results require an expected_count.")
            return self
        if self.expected_count != self.succeeded_count + len(self.missing_items):
            raise ValueError(
                "expected_count must equal succeeded items plus explicitly missing items."
            )
        if self.complete != (self.expected_count == self.succeeded_count):
            raise ValueError("complete must reflect whether succeeded_count equals expected_count.")
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
