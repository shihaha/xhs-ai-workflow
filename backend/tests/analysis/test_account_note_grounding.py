import json
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, text

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    ModelResult,
    StructuredModelRequest,
)
import backend.app.db as db_module
from backend.app.db import Database, canonical_raw_evidence_digest
from backend.app.features.analysis.schemas import AnalysisCreate
from backend.app.features.analysis.service import (
    AnalysisService,
    EvidenceAccountMismatch,
    EvidenceNotFound,
)
from backend.app.features.xhs.models import XhsAccountNoteRecord
from backend.app.features.xhs.service import XhsCollectionService
from backend.app.models.jobs import JobArtifactRecord, JobState
from backend.app.services.jobs import JobService


class _AccountAdapter:
    def note_id_for(self, user_id: str) -> str:
        return f"note-{user_id}"

    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        user_id = str(request.parameters["user_id"])
        note_id = self.note_id_for(user_id)
        return CollectionResult(
            status="succeeded",
            items=[
                CollectionItem(
                    id=f"profile:{user_id}",
                    kind="profile",
                    source_url=f"https://www.xiaohongshu.com/user/profile/{user_id}",
                    raw_evidence={
                        "profile": {"user_id": user_id, "nickname": f"Name {user_id}"},
                        "opaque": f"RAW-PROFILE-{user_id}",
                    },
                    data={
                        "user_id": user_id,
                        "nickname": f"Name {user_id}",
                        "bio": f"Bio {user_id}",
                        "followers_count": 12,
                    },
                ),
                CollectionItem(
                    id=f"note:{note_id}",
                    kind="note",
                    source_url=f"https://www.xiaohongshu.com/explore/{note_id}",
                    raw_evidence={
                        "row": {"note_id": note_id, "user_id": user_id},
                        "opaque": f"RAW-NOTE-{user_id}",
                    },
                    data={
                        "note_id": note_id,
                        "user_id": user_id,
                        "title": f"Title {user_id}",
                        "summary": f"Summary {user_id}",
                        "published_at": "2026-08-18T10:00:00Z",
                        "liked_count": 7,
                    },
                ),
            ],
            expected_count_known=True,
            expected_count=2,
            succeeded_count=2,
            observed_count=2,
            overflow_count=0,
            complete=True,
        )

    def search_notes(self, request: CollectionRequest) -> CollectionResult:
        return CollectionResult(
            status="succeeded",
            items=[],
            expected_count_known=True,
            expected_count=0,
            succeeded_count=0,
            observed_count=0,
            overflow_count=0,
            complete=True,
        )


class _RotatingAccountAdapter(_AccountAdapter):
    def __init__(self, note_ids: list[str]) -> None:
        self.note_ids = iter(note_ids)

    def note_id_for(self, user_id: str) -> str:
        return next(self.note_ids)


class _CompleteCounterAccountAdapter(_AccountAdapter):
    def __init__(self, value: int = 1) -> None:
        self.value = value

    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        result = super().fetch_account(request)
        result.items[0].data.update(
            {
                "followers_count": self.value,
                "following_count": self.value,
                "liked_count": self.value,
                "fans": self.value,
                "follows": self.value,
            }
        )
        result.items[1].data.update(
            {
                "liked_count": self.value,
                "collect_count": self.value,
                "comment_count": self.value,
                "likedCount": self.value,
                "collectCount": self.value,
                "commentCount": self.value,
            }
        )
        return result


class _ModelSpy:
    provider = "provider-neutral-spy"
    model = "structured-model"
    configured = True

    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self.output = output
        self.calls: list[StructuredModelRequest] = []

    def generate_structured(
        self, request: StructuredModelRequest, schema: object
    ) -> ModelResult:
        self.calls.append(request)
        output = self.output or {
            "claims": [
                {
                    "claim": "The public note supports this claim.",
                    "evidence_ids": list(request.evidence_ids),
                }
            ],
            "product_clusters": [],
            "opportunities": [],
        }
        return ModelResult(
            model=self.model,
            output=output,
            raw_evidence={"provider_request_id": "request-1"},
            usage={},
            duration_ms=1,
        )


class _Fixture:
    def __init__(self, tmp_path: Path, *, adapter: object | None = None) -> None:
        self.runtime_dir = tmp_path / "runtime"
        self.database = Database(
            self.runtime_dir / "workbench.sqlite3", runtime_dir=self.runtime_dir
        )
        self.jobs = JobService(self.database, runtime_dir=self.runtime_dir)
        self.collection = XhsCollectionService(
            database=self.database,
            job_service=self.jobs,
            adapter=adapter or _AccountAdapter(),
            runtime_dir=self.runtime_dir,
            submitter=lambda *_args: None,
        )

    def collect(self, user_id: str) -> tuple[str, int]:
        queued = self.collection.submit_account(user_id, 1)
        completed = self.collection.execute(queued.id)
        assert completed is not None and completed.state is JobState.succeeded
        with self.database.session() as session:
            note = session.scalar(
                select(XhsAccountNoteRecord).where(
                    XhsAccountNoteRecord.user_id == user_id
                )
            )
            assert note is not None
            return queued.id, note.id

    def analysis(self, model: _ModelSpy) -> AnalysisService:
        return AnalysisService(
            self.database, model, runtime_dir=self.runtime_dir
        )


def _account_payload(user_id: str, evidence_id: str) -> AnalysisCreate:
    return AnalysisCreate(
        analysis_type="account_report",
        account_user_id=user_id,
        evidence_ids=[evidence_id],
    )


def _artifact_for_job(fixture: _Fixture, job_id: str) -> JobArtifactRecord:
    with fixture.database.session() as session:
        artifact = session.scalar(
            select(JobArtifactRecord).where(JobArtifactRecord.job_id == job_id)
        )
        assert artifact is not None
        session.expunge(artifact)
        return artifact


@contextmanager
def _historical_parent_tamper(
    fixture: _Fixture,
    trigger_name: str,
):
    """Model corruption that predates the now-physical journal guards."""

    with fixture.database.engine.begin() as connection:
        connection.execute(text(f"DROP TRIGGER {trigger_name}"))
    try:
        yield
    finally:
        with fixture.database.engine.begin() as connection:
            connection.execute(text(
                db_module._XHS_ARTIFACT_JOURNAL_TRIGGERS[trigger_name]
            ))


@contextmanager
def _historical_note_fact_tamper(fixture: _Fixture):
    """Model note-row corruption that predates the content-freeze trigger."""

    trigger_name = "ck_xhs_note_immutable_update"
    with fixture.database.engine.begin() as connection:
        connection.execute(text(f"DROP TRIGGER {trigger_name}"))
    try:
        yield
    finally:
        with fixture.database.engine.begin() as connection:
            connection.execute(text(
                db_module._XHS_ACCOUNT_FACT_IMMUTABILITY_TRIGGERS[trigger_name]
            ))


@contextmanager
def _historical_counter_tamper(fixture: _Fixture, entity: str):
    trigger_name = (
        "ck_xhs_profile_immutable_update"
        if entity == "profile"
        else "ck_xhs_note_immutable_update"
    )
    with fixture.database.engine.begin() as connection:
        connection.execute(text(f"DROP TRIGGER {trigger_name}"))
    try:
        yield
    finally:
        with fixture.database.engine.begin() as connection:
            connection.execute(text(
                db_module._XHS_ACCOUNT_FACT_IMMUTABILITY_TRIGGERS[trigger_name]
            ))


_PROFILE_PUBLIC_COUNTER_FIELDS = (
    "followers_count",
    "following_count",
    "liked_count",
    "fans",
    "follows",
)
_NOTE_PUBLIC_COUNTER_FIELDS = (
    "liked_count",
    "collect_count",
    "comment_count",
    "likedCount",
    "collectCount",
    "commentCount",
)
_PUBLIC_COUNTER_TYPE_DRIFT_CASES = [
    ("profile", _PROFILE_PUBLIC_COUNTER_FIELDS, field, replacement)
    for field in _PROFILE_PUBLIC_COUNTER_FIELDS
    for replacement in (1.0, True)
] + [
    ("note", _NOTE_PUBLIC_COUNTER_FIELDS, field, replacement)
    for field in _NOTE_PUBLIC_COUNTER_FIELDS
    for replacement in (1.0, True)
]


def test_discovery_returns_only_account_notes_owned_by_requested_account(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    _, u1_note_id = fixture.collect("u1")
    _, u2_note_id = fixture.collect("u2")

    rows = fixture.analysis(_ModelSpy()).list_evidence(account_user_id="u1")
    all_rows = fixture.analysis(_ModelSpy()).list_evidence()

    assert [row.evidence_id for row in rows] == [f"account-note:{u1_note_id}"]
    assert {row.evidence_id for row in all_rows} == {
        f"account-note:{u1_note_id}",
        f"account-note:{u2_note_id}",
    }
    assert rows[0].kind == "account_note"
    assert rows[0].account_user_id == "u1"
    # A trusted note may enrich an opportunity request, but the service still
    # requires a separate exact-complete shop artifact before calling the model.
    assert rows[0].eligible_for_opportunity is True


def test_unfiltered_discovery_hides_account_and_search_raw_trust_anchors(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    _, note_id = fixture.collect("u1")
    queued = fixture.collection.submit_search("safe", 0)
    completed = fixture.collection.execute(queued.id)
    assert completed is not None and completed.state is JobState.succeeded

    rows = fixture.analysis(_ModelSpy()).list_evidence()

    assert [row.evidence_id for row in rows] == [f"account-note:{note_id}"]
    assert all(
        row.kind not in {"xhs_account_collection_raw", "xhs_note_search_raw"}
        for row in rows
    )


def test_unknown_job_state_fails_closed_in_filtered_and_unfiltered_discovery(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    job_id, note_id = fixture.collect("u1")
    with _historical_parent_tamper(
        fixture,
        "ck_xhs_artifact_journal_job_update",
    ):
        with fixture.database.engine.begin() as connection:
            connection.execute(
                text("UPDATE jobs SET state='retired_legacy_state' WHERE id=:job_id"),
                {"job_id": job_id},
            )
    model = _ModelSpy()
    service = fixture.analysis(model)

    filtered = service.list_evidence(account_user_id="u1")
    unfiltered = service.list_evidence()
    with pytest.raises(EvidenceNotFound):
        service.create(_account_payload("u1", f"account-note:{note_id}"))

    assert [row.evidence_id for row in filtered] == [f"account-note:{note_id}"]
    assert filtered[0].eligible_for_opportunity is False
    assert [row.evidence_id for row in unfiltered] == [f"account-note:{note_id}"]
    assert unfiltered[0].eligible_for_opportunity is False
    assert model.calls == []


def test_cross_account_note_reference_is_rejected_before_model_call(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    _, foreign_note_id = fixture.collect("u2")
    model = _ModelSpy()

    with pytest.raises(EvidenceAccountMismatch):
        fixture.analysis(model).create(
            _account_payload("u1", f"account-note:{foreign_note_id}")
        )

    assert model.calls == []


def test_tampered_raw_artifact_makes_note_unusable_before_model(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    job_id, note_id = fixture.collect("u1")
    artifact = _artifact_for_job(fixture, job_id)
    (fixture.runtime_dir / artifact.path).write_bytes(b"tampered")
    model = _ModelSpy()
    service = fixture.analysis(model)

    discovered = service.list_evidence(account_user_id="u1")
    with pytest.raises(EvidenceNotFound):
        service.create(_account_payload("u1", f"account-note:{note_id}"))

    assert [row.evidence_id for row in discovered] == [f"account-note:{note_id}"]
    assert discovered[0].eligible_for_opportunity is False
    assert model.calls == []


@pytest.mark.parametrize("tamper", ["producer", "kind", "job_type", "job_state"])
def test_untrusted_job_or_artifact_identity_rejects_whole_request(
    tmp_path: Path, tamper: str
) -> None:
    fixture = _Fixture(tmp_path)
    job_id, note_id = fixture.collect("u1")
    trigger_name = (
        "ck_xhs_artifact_journal_job_update"
        if tamper.startswith("job_")
        else "ck_xhs_artifact_journal_artifact_update"
    )
    with _historical_parent_tamper(fixture, trigger_name):
        with fixture.database.session() as session:
            artifact = session.scalar(
                select(JobArtifactRecord).where(JobArtifactRecord.job_id == job_id)
            )
            assert artifact is not None
            if tamper == "producer":
                artifact.producer = "external"
            elif tamper == "kind":
                artifact.kind = "xhs_note_search_raw"
            elif tamper == "job_type":
                artifact.job.type = "manual_note"
            else:
                artifact.job.state = JobState.failed.value
            session.commit()
    model = _ModelSpy()

    with pytest.raises(EvidenceNotFound):
        fixture.analysis(model).create(
            _account_payload("u1", f"account-note:{note_id}")
        )

    assert model.calls == []


@pytest.mark.parametrize(
    "tamper",
    ["path", "sha256", "size_bytes", "metadata_count", "metadata_artifact_id"],
)
def test_path_hash_size_and_metadata_must_match_the_physical_artifact(
    tmp_path: Path, tamper: str
) -> None:
    fixture = _Fixture(tmp_path)
    job_id, note_id = fixture.collect("u1")
    with _historical_parent_tamper(
        fixture,
        "ck_xhs_artifact_journal_artifact_update",
    ):
        with fixture.database.session() as session:
            artifact = session.scalar(
                select(JobArtifactRecord).where(JobArtifactRecord.job_id == job_id)
            )
            assert artifact is not None
            metadata = dict(artifact.metadata_json)
            if tamper == "path":
                artifact.path = "evidence/xhs/foreign.json"
            elif tamper == "sha256":
                metadata["sha256"] = "0" * 64
            elif tamper == "size_bytes":
                metadata["size_bytes"] = metadata["size_bytes"] + 1
            elif tamper == "metadata_count":
                metadata["succeeded_note_count"] = 0
            else:
                metadata["artifact_id"] = metadata["artifact_id"] + 1
            artifact.metadata_json = metadata
            session.commit()
    model = _ModelSpy()

    with pytest.raises(EvidenceNotFound):
        fixture.analysis(model).create(
            _account_payload("u1", f"account-note:{note_id}")
        )

    assert model.calls == []


def test_persisted_raw_digest_must_match_the_bound_artifact_item(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    _, note_id = fixture.collect("u1")
    with _historical_note_fact_tamper(fixture):
        with fixture.database.session() as session:
            note = session.get(XhsAccountNoteRecord, note_id)
            assert note is not None
            replacement = {
                "row": {"note_id": note.note_id, "user_id": note.user_id},
                "opaque": "database-only-replacement",
            }
            digest = canonical_raw_evidence_digest(replacement)
            assert digest is not None
            note.raw_evidence = replacement
            note.raw_digest = digest
            session.commit()
    model = _ModelSpy()

    with pytest.raises(EvidenceNotFound):
        fixture.analysis(model).create(
            _account_payload("u1", f"account-note:{note_id}")
        )

    assert model.calls == []


@pytest.mark.parametrize(
    ("entity", "fields", "changed_field", "replacement"),
    _PUBLIC_COUNTER_TYPE_DRIFT_CASES,
)
def test_analysis_xhs_gate_rejects_public_counter_type_drift_before_model(
    tmp_path: Path,
    entity: str,
    fields: tuple[str, ...],
    changed_field: str,
    replacement: object,
) -> None:
    fixture = _Fixture(tmp_path, adapter=_CompleteCounterAccountAdapter())
    _, note_id = fixture.collect("u1")
    payload: dict[str, object] = {field: 1 for field in fields}
    payload[changed_field] = replacement
    table = "xhs_account_profiles" if entity == "profile" else "xhs_account_notes"
    column = "public_stats_json" if entity == "profile" else "public_interactions_json"
    predicate = "user_id='u1'" if entity == "profile" else "note_id='note-u1'"
    with _historical_counter_tamper(fixture, entity):
        with fixture.database.engine.begin() as connection:
            connection.execute(
                text(f"UPDATE {table} SET {column}=:value WHERE {predicate}"),
                {"value": json.dumps(payload)},
            )
    model = _ModelSpy()
    service = fixture.analysis(model)
    evidence_id = f"account-note:{note_id}"

    discovered = service.list_evidence(account_user_id="u1")
    with pytest.raises(EvidenceNotFound):
        service.create(_account_payload("u1", evidence_id))

    assert [row.evidence_id for row in discovered] == [evidence_id]
    assert discovered[0].eligible_for_opportunity is False
    assert service.list() == []
    assert model.calls == []


@pytest.mark.parametrize(
    ("entity", "fields", "mutation"),
    [
        ("profile", _PROFILE_PUBLIC_COUNTER_FIELDS, "null"),
        ("profile", _PROFILE_PUBLIC_COUNTER_FIELDS, "nested"),
        ("profile", _PROFILE_PUBLIC_COUNTER_FIELDS, "missing"),
        ("profile", _PROFILE_PUBLIC_COUNTER_FIELDS, "extra"),
        ("note", _NOTE_PUBLIC_COUNTER_FIELDS, "null"),
        ("note", _NOTE_PUBLIC_COUNTER_FIELDS, "nested"),
        ("note", _NOTE_PUBLIC_COUNTER_FIELDS, "missing"),
        ("note", _NOTE_PUBLIC_COUNTER_FIELDS, "extra"),
    ],
)
def test_analysis_xhs_gate_rejects_malformed_public_counter_shapes_before_model(
    tmp_path: Path,
    entity: str,
    fields: tuple[str, ...],
    mutation: str,
) -> None:
    fixture = _Fixture(tmp_path, adapter=_CompleteCounterAccountAdapter())
    _, note_id = fixture.collect("u1")
    payload: dict[str, object] = {field: 1 for field in fields}
    if mutation == "null":
        payload[fields[0]] = None
    elif mutation == "nested":
        payload[fields[0]] = {"value": 1}
    elif mutation == "missing":
        payload.pop(fields[0])
    else:
        payload["unexpected_count"] = 1
    table = "xhs_account_profiles" if entity == "profile" else "xhs_account_notes"
    column = "public_stats_json" if entity == "profile" else "public_interactions_json"
    predicate = "user_id='u1'" if entity == "profile" else "note_id='note-u1'"
    with _historical_counter_tamper(fixture, entity):
        with fixture.database.engine.begin() as connection:
            connection.execute(
                text(f"UPDATE {table} SET {column}=:value WHERE {predicate}"),
                {"value": json.dumps(payload)},
            )
    model = _ModelSpy()
    service = fixture.analysis(model)
    evidence_id = f"account-note:{note_id}"

    discovered = service.list_evidence(account_user_id="u1")
    with pytest.raises(EvidenceNotFound):
        service.create(_account_payload("u1", evidence_id))

    assert [row.evidence_id for row in discovered] == [evidence_id]
    assert discovered[0].eligible_for_opportunity is False
    assert service.list() == []
    assert model.calls == []


def test_analysis_xhs_gate_accepts_exact_large_integer_public_counters(
    tmp_path: Path,
) -> None:
    large_integer = 9_007_199_254_740_993
    fixture = _Fixture(
        tmp_path,
        adapter=_CompleteCounterAccountAdapter(large_integer),
    )
    _, note_id = fixture.collect("u1")
    model = _ModelSpy()

    created = fixture.analysis(model).create(
        _account_payload("u1", f"account-note:{note_id}")
    )

    assert created.status == "succeeded"
    assert len(model.calls) == 1
    prompt = json.loads(model.calls[0].user_prompt)
    facts = prompt["allowed_evidence"][0]["facts"]
    assert facts["profile"]["public_stats"] == {
        "followers_count": large_integer,
        "following_count": large_integer,
        "liked_count": large_integer,
        "fans": large_integer,
        "follows": large_integer,
    }
    assert facts["note"]["public_interactions"] == {
        "liked_count": large_integer,
        "collect_count": large_integer,
        "comment_count": large_integer,
        "likedCount": large_integer,
        "collectCount": large_integer,
        "commentCount": large_integer,
    }


def test_account_note_model_facts_are_public_normalized_and_claims_can_cite_them(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    job_id, note_id = fixture.collect("u1")
    fixture.collect("u2")
    evidence_id = f"account-note:{note_id}"
    model = _ModelSpy()

    created = fixture.analysis(model).create(_account_payload("u1", evidence_id))

    assert created.status == "succeeded"
    assert created.output is not None
    assert created.output.claims[0].evidence_ids == [evidence_id]
    assert len(model.calls) == 1
    prompt = json.loads(model.calls[0].user_prompt)
    assert prompt["allowed_evidence"] == [
        {
            "evidence_id": evidence_id,
            "kind": "account_note",
            "account_user_id": "u1",
            "facts": {
                "profile": {
                    "user_id": "u1",
                    "source_url": "https://www.xiaohongshu.com/user/profile/u1",
                    "nickname": "Name u1",
                    "bio": "Bio u1",
                    "public_stats": {"followers_count": 12},
                },
                "note": {
                    "note_id": "note-u1",
                    "source_url": "https://www.xiaohongshu.com/explore/note-u1",
                    "title": "Title u1",
                    "summary": "Summary u1",
                    "published_at": "2026-08-18T10:00:00Z",
                    "public_interactions": {"liked_count": 7},
                },
            },
        }
    ]
    serialized = model.calls[0].user_prompt
    assert "RAW-NOTE" not in serialized
    assert "RAW-PROFILE" not in serialized
    assert "Title u2" not in serialized
    assert job_id not in serialized
    assert str(fixture.runtime_dir) not in serialized


def test_model_cannot_cite_a_trusted_note_not_supplied_in_this_request(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    _, requested_note_id = fixture.collect("u1")
    _, extra_note_id = fixture.collect("u2")
    extra_id = f"account-note:{extra_note_id}"
    model = _ModelSpy(
        {
            "claims": [{"claim": "foreign", "evidence_ids": [extra_id]}],
            "product_clusters": [],
            "opportunities": [],
        }
    )

    created = fixture.analysis(model).create(
        _account_payload("u1", f"account-note:{requested_note_id}")
    )

    assert created.status == "failed"
    assert created.error_category == "evidence_grounding_failed"


def test_note_only_never_replaces_complete_shop_gate_for_opportunity(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    _, note_id = fixture.collect("u1")
    model = _ModelSpy()

    created = fixture.analysis(model).create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_ids=["u1"],
            evidence_ids=[f"account-note:{note_id}"],
        )
    )

    assert created.status == "needs_human"
    assert created.error_category == "deep_verification_incomplete"
    assert model.calls == []


def test_unknown_and_stale_account_note_ids_fail_before_model(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    _, note_id = fixture.collect("u1")
    model = _ModelSpy()
    service = fixture.analysis(model)

    with pytest.raises(EvidenceNotFound):
        service.create(_account_payload("u1", "account-note:999999"))
    payload = _account_payload("u1", f"account-note:{note_id}")
    with fixture.database.session() as session:
        session.execute(
            delete(XhsAccountNoteRecord).where(XhsAccountNoteRecord.id == note_id)
        )
        session.commit()
    with pytest.raises(EvidenceNotFound):
        service.create(payload)

    assert model.calls == []


def test_recollection_never_rebinds_a_historical_account_note_citation(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(
        tmp_path,
        adapter=_RotatingAccountAdapter(["old-note", "new-note"]),
    )
    _, old_note_row_id = fixture.collect("u1")
    old_evidence_id = f"account-note:{old_note_row_id}"
    historical = fixture.analysis(_ModelSpy()).create(
        _account_payload("u1", old_evidence_id)
    )

    _, new_note_row_id = fixture.collect("u1")
    new_evidence_id = f"account-note:{new_note_row_id}"
    stale_model = _ModelSpy()
    service = fixture.analysis(stale_model)

    with pytest.raises(EvidenceNotFound):
        service.create(_account_payload("u1", old_evidence_id))

    assert new_note_row_id > old_note_row_id
    assert new_evidence_id != old_evidence_id
    assert [row.evidence_id for row in service.list_evidence(account_user_id="u1")] == [
        new_evidence_id
    ]
    assert service.get(historical.id).evidence_ids == [old_evidence_id]
    assert stale_model.calls == []


def test_concurrent_recollection_cannot_rebind_an_inflight_analysis_citation(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(
        tmp_path,
        adapter=_RotatingAccountAdapter(["old-note", "new-note"]),
    )
    _, old_note_row_id = fixture.collect("u1")
    old_evidence_id = f"account-note:{old_note_row_id}"
    entered_model = Event()
    release_model = Event()

    class BlockingModel(_ModelSpy):
        def generate_structured(
            self, request: StructuredModelRequest, schema: object
        ) -> ModelResult:
            entered_model.set()
            assert release_model.wait(2)
            return super().generate_structured(request, schema)

    model = BlockingModel()
    service = fixture.analysis(model)
    result: dict[str, object] = {}

    def create_analysis() -> None:
        result["analysis"] = service.create(
            _account_payload("u1", old_evidence_id)
        )

    thread = Thread(target=create_analysis)
    thread.start()
    assert entered_model.wait(1)
    _, new_note_row_id = fixture.collect("u1")
    release_model.set()
    thread.join(2)

    assert not thread.is_alive()
    assert new_note_row_id > old_note_row_id
    created = result["analysis"]
    assert getattr(created, "status") == "succeeded"
    assert getattr(created, "evidence_ids") == [old_evidence_id]
    with pytest.raises(EvidenceNotFound):
        service.create(_account_payload("u1", old_evidence_id))
    assert [row.evidence_id for row in service.list_evidence(account_user_id="u1")] == [
        f"account-note:{new_note_row_id}"
    ]


@pytest.mark.parametrize("poison_id", [0, -1])
def test_noncanonical_persisted_note_id_is_never_discovered_or_used(
    tmp_path: Path,
    poison_id: int,
) -> None:
    fixture = _Fixture(tmp_path)
    _, note_row_id = fixture.collect("u1")
    with _historical_note_fact_tamper(fixture):
        with fixture.database.engine.begin() as connection:
            connection.execute(text("PRAGMA ignore_check_constraints=ON"))
            connection.execute(
                text("UPDATE xhs_account_notes SET id=:poison_id WHERE id=:note_id"),
                {"poison_id": poison_id, "note_id": note_row_id},
            )
    model = _ModelSpy()
    service = fixture.analysis(model)
    payload = AnalysisCreate.model_construct(
        analysis_type="account_report",
        account_user_id="u1",
        account_user_ids=[],
        evidence_ids=[f"account-note:{poison_id}"],
    )

    assert service.list_evidence(account_user_id="u1") == []
    with pytest.raises(EvidenceNotFound):
        service.create(payload)
    assert model.calls == []


@pytest.mark.parametrize("evidence_id", ["account-note:0", "account-note:01", "account-note:+1"])
def test_account_note_ids_must_be_canonical_sqlite_identities(evidence_id: str) -> None:
    with pytest.raises(ValidationError):
        _account_payload("u1", evidence_id)


def test_duplicate_account_note_ids_are_rejected_before_service_or_model() -> None:
    with pytest.raises(ValidationError):
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="u1",
            evidence_ids=["account-note:1", "account-note:1"],
        )
