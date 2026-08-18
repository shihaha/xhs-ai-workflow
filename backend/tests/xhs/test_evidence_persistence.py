from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.adapters.contracts import CollectionItem, CollectionResult, MissingCollectionItem
from backend.app.db import Database, canonical_raw_evidence_digest
from backend.app.features.xhs.models import XhsAccountNoteRecord, XhsAccountProfileRecord
from backend.app.features.xhs.schemas import (
    AccountEvidenceBinding,
    AccountEvidencePersistenceError,
    persist_exact_account_result,
)
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState


def _result(*, status: str = "succeeded", complete: bool = True) -> CollectionResult:
    items = [
        CollectionItem(
            id="profile:user-1",
            kind="profile",
            source_url="https://www.xiaohongshu.com/user/profile/user-1",
            raw_evidence={"profile": {"id": "user-1", "nickname": "Alice"}},
            data={"user_id": "user-1", "nickname": "Alice"},
        ),
        CollectionItem(
            id="note:note-1",
            kind="note",
            source_url="https://www.xiaohongshu.com/explore/note-1",
            raw_evidence={"row": {"id": "note-1", "title": "First"}},
            data={"note_id": "note-1", "title": "First"},
        ),
    ]
    return CollectionResult(
        status=status,  # type: ignore[arg-type]
        items=items if status == "succeeded" else [],
        expected_count_known=True,
        expected_count=2,
        succeeded_count=2 if status == "succeeded" else 0,
        observed_count=2 if status == "succeeded" else 0,
        missing_items=(
            []
            if status == "succeeded"
            else [
                MissingCollectionItem(
                    reference=f"missing-{index}",
                    reason="expected_item_not_observed",
                    raw_evidence={"index": index},
                )
                for index in range(2)
            ]
        ),
        overflow_count=0,
        complete=complete,
    )


def _reserved_binding(database: Database) -> AccountEvidenceBinding:
    now = datetime.now(UTC).replace(tzinfo=None)
    job_id = str(uuid4())
    with database.session() as session:
        session.add(JobRecord(
            id=job_id,
            type="xhs_account_collection",
            input_data={},
            state=JobState.running,
            progress_current=0,
            progress_total=2,
            created_at=now,
            updated_at=now,
            started_at=now,
        ))
        session.flush()
        artifact = JobArtifactRecord(
            job_id=job_id,
            kind="xhs_account_collection_result",
            producer="xhs_account_worker_v1",
            path="evidence/xhs/result.json",
            metadata_json={},
            created_at=now,
        )
        session.add(artifact)
        session.commit()
        return AccountEvidenceBinding(
            collection_job_id=job_id,
            collection_artifact_id=artifact.id,
            collected_at=now,
        )


def test_exact_result_persists_profile_and_notes_bound_to_reserved_evidence(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "facts.sqlite3")
    binding = _reserved_binding(database)
    result = _result()

    with database.session() as session:
        persisted = persist_exact_account_result(session, result=result, binding=binding)
        session.commit()

    with database.session() as session:
        profile = session.get(XhsAccountProfileRecord, "user-1")
        notes = session.scalars(select(XhsAccountNoteRecord)).all()
    assert profile is not None
    assert profile.collection_job_id == binding.collection_job_id
    assert profile.collection_artifact_id == binding.collection_artifact_id
    assert profile.raw_digest == canonical_raw_evidence_digest(result.items[0].raw_evidence)
    assert [note.note_id for note in notes] == ["note-1"]
    assert notes[0].user_id == profile.user_id
    assert notes[0].collection_artifact_id == profile.collection_artifact_id
    assert persisted.profile.user_id == "user-1"


def test_incomplete_result_persists_no_normalized_account_fact(tmp_path: Path) -> None:
    database = Database(tmp_path / "partial.sqlite3")
    binding = _reserved_binding(database)
    partial = _result(status="partial", complete=False)

    with database.session() as session:
        with pytest.raises(AccountEvidencePersistenceError, match="exact complete"):
            persist_exact_account_result(session, result=partial, binding=binding)
        session.commit()

    with database.session() as session:
        assert session.scalars(select(XhsAccountProfileRecord)).all() == []
        assert session.scalars(select(XhsAccountNoteRecord)).all() == []


def test_mismatched_artifact_job_cannot_persist_account_evidence(tmp_path: Path) -> None:
    database = Database(tmp_path / "mismatched.sqlite3")
    binding = _reserved_binding(database)
    wrong = binding.model_copy(update={"collection_job_id": str(uuid4())})

    with database.session() as session:
        with pytest.raises(AccountEvidencePersistenceError, match="reserved"):
            persist_exact_account_result(session, result=_result(), binding=wrong)
        session.commit()

    with database.session() as session:
        assert session.scalars(select(XhsAccountProfileRecord)).all() == []
