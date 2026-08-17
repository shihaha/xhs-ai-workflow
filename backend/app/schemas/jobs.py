"""HTTP schemas for the factual durable-jobs API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.app.models.jobs import JobState


class JobCreate(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    input: dict[str, Any] = Field(default_factory=dict)
    progress_current: int = Field(default=0, ge=0)
    progress_total: int | None = Field(default=None, ge=0)
    current_stage: str | None = Field(default=None, max_length=255)


class JobTransition(BaseModel):
    state: JobState
    progress_current: int | None = Field(default=None, ge=0)
    progress_total: int | None = Field(default=None, ge=0)
    current_stage: str | None = Field(default=None, max_length=255)
    error_category: str | None = Field(default=None, max_length=100)


class JobLogCreate(BaseModel):
    level: str = Field(min_length=1, max_length=32)
    message: str = Field(min_length=1)


class JobArtifactCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobLogRead(BaseModel):
    level: str
    message: str


class JobArtifactRead(BaseModel):
    kind: str
    producer: str
    path: str
    metadata: dict[str, Any]


class JobRead(BaseModel):
    id: str
    type: str
    input: dict[str, Any]
    state: JobState
    progress_current: int
    progress_total: int | None
    current_stage: str | None
    error_category: str | None
    retry_count: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    lease_expires_at: datetime | None
    logs: list[JobLogRead]
    artifacts: list[JobArtifactRead]
