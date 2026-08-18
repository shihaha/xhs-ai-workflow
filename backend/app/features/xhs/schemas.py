"""Strict reads and one-transaction persistence for XHS account evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.app.adapters.contracts import CollectionItem, CollectionResult
from backend.app.db import canonical_raw_evidence_digest
from backend.app.features.xhs.constants import (
    ACCOUNT_COLLECTION_ARTIFACT_KIND,
    ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
    ACCOUNT_COLLECTION_JOB_TYPE,
)
from backend.app.features.xhs.models import XhsAccountNoteRecord, XhsAccountProfileRecord
from backend.app.models.jobs import JobArtifactRecord, JobRecord


class _StrictRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class AccountEvidenceBinding(BaseModel):
    """A Task3-reserved artifact identity for every normalized fact written now."""

    model_config = ConfigDict(extra="forbid", strict=True)

    collection_job_id: str = Field(min_length=1, max_length=36)
    collection_artifact_id: int = Field(gt=0)
    collected_at: datetime


class XhsAccountProfileRead(_StrictRead):
    user_id: str
    source_url: str
    nickname: str | None
    bio: str | None
    public_stats: dict[str, Any] = Field(validation_alias="public_stats_json")
    raw_evidence: dict[str, Any]
    raw_digest: str
    collection_job_id: str
    collection_artifact_id: int
    collected_at: datetime


class XhsAccountNoteRead(_StrictRead):
    id: int
    note_id: str
    user_id: str
    source_url: str
    title: str | None
    summary: str | None
    published_at: str | None
    public_interactions: dict[str, Any] = Field(validation_alias="public_interactions_json")
    raw_evidence: dict[str, Any]
    raw_digest: str
    collection_job_id: str
    collection_artifact_id: int
    collected_at: datetime


class PersistedAccountResult(_StrictRead):
    profile: XhsAccountProfileRead
    notes: list[XhsAccountNoteRead]


class AccountEvidencePersistenceError(ValueError):
    """The caller did not present an exact result tied to a reserved artifact."""


def persist_exact_account_result(
    session: Session,
    *,
    result: CollectionResult,
    binding: AccountEvidenceBinding,
) -> PersistedAccountResult:
    """Write one complete profile-plus-notes snapshot without committing the caller's session.

    Task3 owns the job state transition and artifact reservation. This primitive only
    accepts the exact `fetch_account` result and proves that the passed artifact
    belongs to the passed job before replacing the account's complete note snapshot.
    """

    profile_item, note_items = _exact_account_items(result)
    artifact = session.get(JobArtifactRecord, binding.collection_artifact_id)
    job = session.get(JobRecord, binding.collection_job_id)
    if (
        job is None
        or artifact is None
        or artifact.job_id != binding.collection_job_id
        or job.type != ACCOUNT_COLLECTION_JOB_TYPE
        or artifact.kind != ACCOUNT_COLLECTION_ARTIFACT_KIND
        or artifact.producer != ACCOUNT_COLLECTION_ARTIFACT_PRODUCER
    ):
        raise AccountEvidencePersistenceError(
            "Account evidence requires a reserved artifact belonging to its collection job."
        )
    user_id = _profile_user_id(profile_item)
    profile = session.get(XhsAccountProfileRecord, user_id)
    if profile is None:
        profile = XhsAccountProfileRecord(user_id=user_id)
        session.add(profile)
    _apply_profile_item(profile, profile_item, binding)
    session.execute(delete(XhsAccountNoteRecord).where(XhsAccountNoteRecord.user_id == user_id))
    session.flush()
    notes: list[XhsAccountNoteRecord] = []
    for item in note_items:
        note = XhsAccountNoteRecord(note_id=_note_id(item, user_id), user_id=user_id)
        _apply_note_item(note, item, binding)
        session.add(note)
        notes.append(note)
    session.flush()
    return PersistedAccountResult(
        profile=XhsAccountProfileRead.model_validate(profile),
        notes=[XhsAccountNoteRead.model_validate(note) for note in notes],
    )


def _exact_account_items(result: CollectionResult) -> tuple[CollectionItem, list[CollectionItem]]:
    if (
        result.status != "succeeded"
        or not result.complete
        or not result.expected_count_known
        or result.expected_count is None
        or result.expected_count != result.succeeded_count
        or result.expected_count != len(result.items)
        or result.rejected_items
        or result.missing_items
        or result.overflow_count
    ):
        raise AccountEvidencePersistenceError(
            "Normalized account facts require an exact complete collection result."
        )
    profiles = [item for item in result.items if item.kind == "profile"]
    notes = [item for item in result.items if item.kind == "note"]
    if len(profiles) != 1 or len(notes) + 1 != result.expected_count:
        raise AccountEvidencePersistenceError(
            "Exact fetch_account results require one profile plus N notes."
        )
    if len(profiles) + len(notes) != len(result.items):
        raise AccountEvidencePersistenceError("Account results contain an unsupported item kind.")
    _profile_user_id(profiles[0])
    owner_id = _profile_user_id(profiles[0])
    note_ids = [_note_id(item, owner_id) for item in notes]
    if len(note_ids) != len(set(note_ids)):
        raise AccountEvidencePersistenceError("Account notes must have distinct note identities.")
    return profiles[0], notes


def _profile_user_id(item: CollectionItem) -> str:
    user_id = item.data.get("user_id")
    if (
        not isinstance(user_id, str)
        or not user_id
        or len(user_id) > 500
        or item.id != f"profile:{user_id}"
    ):
        raise AccountEvidencePersistenceError("Profile identity is not exact.")
    return user_id


def _note_id(item: CollectionItem, owner_id: str) -> str:
    note_id = item.data.get("note_id")
    note_owner_id = item.data.get("user_id")
    if (
        not isinstance(note_id, str)
        or not note_id
        or len(note_id) > 500
        or item.id != f"note:{note_id}"
        or not isinstance(note_owner_id, str)
        or not note_owner_id
        or note_owner_id != owner_id
    ):
        raise AccountEvidencePersistenceError("Note identity or owner is not exact.")
    return note_id


def _apply_profile_item(
    record: XhsAccountProfileRecord,
    item: CollectionItem,
    binding: AccountEvidenceBinding,
) -> None:
    record.nickname = _public_text(item.data, "nickname")
    record.bio = _public_text(item.data, "bio", "description", "desc")
    record.public_stats_json = _public_numbers(
        item.data, "followers_count", "following_count", "liked_count", "fans", "follows"
    )
    _apply_evidence(record, item, binding)


def _apply_note_item(
    record: XhsAccountNoteRecord,
    item: CollectionItem,
    binding: AccountEvidenceBinding,
) -> None:
    record.title = _public_text(item.data, "title")
    record.summary = _public_text(item.data, "summary", "description", "desc")
    record.published_at = _public_text(item.data, "published_at", "publish_time", "publishTime")
    record.public_interactions_json = _public_numbers(
        item.data, "liked_count", "collect_count", "comment_count", "likedCount", "collectCount", "commentCount"
    )
    _apply_evidence(record, item, binding)


def _apply_evidence(
    record: XhsAccountProfileRecord | XhsAccountNoteRecord,
    item: CollectionItem,
    binding: AccountEvidenceBinding,
) -> None:
    digest = canonical_raw_evidence_digest(item.raw_evidence)
    if digest is None:
        raise AccountEvidencePersistenceError("Raw collection evidence must be a non-empty JSON object.")
    record.source_url = str(item.source_url)
    record.raw_evidence = dict(item.raw_evidence)
    record.raw_digest = digest
    record.collection_job_id = binding.collection_job_id
    record.collection_artifact_id = binding.collection_artifact_id
    record.collected_at = binding.collected_at


def _public_text(data: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _public_numbers(data: dict[str, Any], *names: str) -> dict[str, int]:
    return {
        name: value
        for name in names
        if isinstance((value := data.get(name)), int) and not isinstance(value, bool) and value >= 0
    }
