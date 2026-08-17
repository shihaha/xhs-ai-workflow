"""SQLAlchemy records for durable workbench jobs and their evidence."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    needs_human = "needs_human"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[JobState] = mapped_column(String(32), nullable=False, default=JobState.queued)
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_stage: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    logs: Mapped[list["JobLogRecord"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobLogRecord.id"
    )
    artifacts: Mapped[list["JobArtifactRecord"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobArtifactRecord.id"
    )


class JobLogRecord(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    job: Mapped[JobRecord] = relationship(back_populates="logs")


class JobArtifactRecord(Base):
    __tablename__ = "job_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    producer: Mapped[str] = mapped_column(
        String(64), nullable=False, default="external", server_default="external"
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    job: Mapped[JobRecord] = relationship(back_populates="artifacts")
