"""Strict projections for tutorial benchmark-note collection."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from backend.app.features.content_research.schemas import StrictModel


class BenchmarkSearchCreate(StrictModel):
    keyword_item_id: str = Field(min_length=1, max_length=36)
    stage: Literal["probe", "full"]


class BenchmarkSearchNoteRead(StrictModel):
    note_id: str
    source_url: str
    title: str | None = None
    summary: str | None = None
    user_id: str | None = None


class BenchmarkSearchRead(StrictModel):
    id: str
    dossier_id: str
    keyword_run_id: str
    keyword_item_id: str
    stage: Literal["probe", "full"]
    attempt: int
    keyword: str
    expected_count: int
    xhs_job_id: str
    job_state: Literal[
        "queued", "running", "needs_human", "succeeded", "failed", "cancelled"
    ]
    progress_current: int
    progress_total: int | None
    error_category: str | None
    result_status: Literal["pending", "trusted", "untrusted"]
    succeeded_count: int | None = None
    artifact_id: int | None = None
    collected_at: datetime | None = None
    items: list[BenchmarkSearchNoteRead] = Field(default_factory=list)
    created_at: datetime


class BenchmarkNoteRead(StrictModel):
    note_id: str
    source_url: str
    title: str | None = None
    summary: str | None = None
    user_id: str | None = None
    source_search_ids: list[str]
    source_keyword_item_ids: list[str]
    source_keywords: list[str]


class BenchmarkOverviewRead(StrictModel):
    dossier_id: str
    current_keyword_run_id: str | None
    searches: list[BenchmarkSearchRead]
    unique_full_notes: list[BenchmarkNoteRead]
    unique_full_note_count: int
