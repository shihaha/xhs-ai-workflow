import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    ModelResult,
    StructuredModelRequest,
)
import backend.app.db as db_module
import backend.app.features.analysis.service as analysis_module
from backend.app.db import Database, canonical_raw_evidence_digest
from backend.app.features.analysis.models import OpportunityRecord
from backend.app.features.analysis.schemas import AnalysisCreate, OpportunityReviewCreate
from backend.app.features.analysis.service import (
    AnalysisService,
    EvidenceAccountMismatch,
    EvidenceNotFound,
    OpportunityStateError,
)
from backend.app.features.xhs.models import XhsAccountNoteRecord
from backend.app.features.xhs.service import XhsCollectionService
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState
from backend.app.services.jobs import JobService


def test_analysis_uses_the_sqlite_bounded_windows_volume_identity() -> None:
    metadata = SimpleNamespace(
        st_dev=12_692_409_288_425_916_732,
        st_ino=17_732_923_534_538_552,
        st_size=232_032,
        st_mtime_ns=1_787_164_465_114_175_000,
    )

    identity = analysis_module._file_identity(metadata)

    assert 0 <= identity[0] <= 9_223_372_036_854_775_807


def test_model_prompt_exposes_cross_account_grounding_contract() -> None:
    prompt = analysis_module._system_prompt()

    assert "supporting_accounts must cover every requested account exactly once" in prompt
    assert "shop_evidence_ids and note_evidence_ids must belong to that account" in prompt
    assert "include every supporting evidence ID in the opportunity evidence_ids" in prompt
    assert "same sufficiently specific, actionable market demand" in prompt
    assert "Broad umbrella needs" in prompt
    assert "opportunities=[]" in prompt


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


class _LatestTenAccountAdapter(_AccountAdapter):
    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        user_id = str(request.parameters["user_id"])
        profile = super().fetch_account(request).items[0]
        notes = [
            CollectionItem(
                id=f"note:latest-{index}",
                kind="note",
                source_url=f"https://www.xiaohongshu.com/explore/latest-{index}",
                raw_evidence={
                    "row": {"note_id": f"latest-{index}", "user_id": user_id}
                },
                data={"note_id": f"latest-{index}", "user_id": user_id},
            )
            for index in range(10)
        ]
        return CollectionResult(
            status="partial",
            detail="expected_count_unknown",
            raw_evidence={
                "latest_sample": {
                    "sample_limit": 10,
                    "available_count_observed": 30,
                    "completeness": "bounded_sample",
                }
            },
            items=[profile, *notes],
            expected_count_known=False,
            succeeded_count=11,
            observed_count=11,
            raw_observation_count=11,
            overflow_count=0,
            complete=False,
        )


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
                ).order_by(XhsAccountNoteRecord.id.desc())
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


def test_latest_ten_account_sample_is_trusted_without_claiming_full_history(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path, adapter=_LatestTenAccountAdapter())
    queued = fixture.collection.submit_latest_account_sample("sample-user")

    completed = fixture.collection.execute(queued.id)
    evidence = fixture.analysis(_ModelSpy()).list_evidence(
        account_user_id="sample-user"
    )
    trusted_notes = [
        item
        for item in evidence
        if item.kind == "account_note" and item.eligible_for_opportunity
    ]

    assert completed is not None and completed.state is JobState.succeeded
    assert len(trusted_notes) == 10


def _complete_shop_artifact(fixture: _Fixture, account_user_id: str) -> str:
    now = datetime.now(UTC).replace(tzinfo=None)
    with fixture.database.session() as session:
        job = JobRecord(
            type="android_shop_collection",
            input_data={"account_user_id": account_user_id, "expected_count": 1},
            state=JobState.succeeded,
            progress_current=1,
            progress_total=1,
            current_stage="shop_complete",
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        source_url = f"https://www.xiaohongshu.com/goods/product-{account_user_id}"
        result = {
            "job_id": job.id,
            "status": "succeeded",
            "detail": None,
            "selector_profile_version": "xhs-shop-v1",
            "expected_count": 1,
            "discovered_count": 1,
            "collected_count": 1,
            "raw_observation_count": 1,
            "duplicate_observation_count": 0,
            "succeeded_count": 1,
            "missing_count": 0,
            "missing_items": [],
            "collection_missing_count": 0,
            "collection_missing_items": [],
            "rejected_count": 0,
            "rejected_items": [],
            "overflow_count": 0,
            "evidence_artifacts": [
                f"evidence/shops/{job.id}/product_1_image_1.jpg",
                f"evidence/shops/{job.id}/product_1_manifest.json",
            ],
            "items": [{
                "id": f"product-{account_user_id}",
                "kind": "shop_product",
                "source_url": source_url,
                "raw_evidence": {"title": f"Product {account_user_id}"},
                "data": {"title": f"Product {account_user_id}"},
            }],
            "verification": {
                "expected_count": 1,
                "discovered_count": 1,
                "succeeded_count": 1,
                "missing_count": 0,
                "missing_items": [],
                "overflow_count": 0,
                "issues": [],
                "complete": True,
            },
            "complete": True,
        }
        relative_path = f"evidence/shops/{job.id}/result.json"
        target = fixture.runtime_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        artifact = JobArtifactRecord(
            job_id=job.id,
            kind="shop_collection_result",
            producer="android_shop_worker_v1",
            path=relative_path,
            metadata_json={"result": result},
            created_at=now,
        )
        session.add(artifact)
        session.commit()
        return f"artifact:{artifact.id}"


def _evidence_sample_shop_artifact(
    fixture: _Fixture, account_user_id: str
) -> str:
    now = datetime.now(UTC).replace(tzinfo=None)
    with fixture.database.session() as session:
        gate = JobRecord(
            type="android_shop_collection",
            input_data={
                "account_user_id": account_user_id,
                "collection_mode": "preflight",
            },
            state=JobState.succeeded,
            progress_current=3,
            progress_total=3,
            current_stage="scope_gate_in_scope",
            created_at=now,
            updated_at=now,
        )
        session.add(gate)
        session.flush()
        gate_result = {
            "job_id": gate.id,
            "account_user_id": account_user_id,
            "classification": "in_scope",
            "deep_collection_allowed": True,
        }
        gate_relative = f"evidence/shops/{gate.id}/scope-gate.json"
        gate_path = fixture.runtime_dir / gate_relative
        gate_path.parent.mkdir(parents=True)
        gate_path.write_text(
            json.dumps(gate_result, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        session.add(JobArtifactRecord(
            job_id=gate.id,
            kind="shop_scope_gate_result",
            producer="external",
            path=gate_relative,
            metadata_json={
                "result": gate_result,
                "sha256": hashlib.sha256(gate_path.read_bytes()).hexdigest(),
            },
            created_at=now,
        ))
        job = JobRecord(
            type="android_shop_collection",
            input_data={
                "account_user_id": account_user_id,
                "expected_count": 3,
                "available_count_observed": 18,
                "collection_mode": "evidence_sample",
                "product_sample_limit": 3,
                "scope_gate_job_id": gate.id,
                "test_override": False,
            },
            state=JobState.succeeded,
            progress_current=3,
            progress_total=3,
            current_stage="shop_evidence_sample_complete",
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        sample_dir = fixture.runtime_dir / "evidence" / "shops" / job.id / "sample-products"
        sample_dir.mkdir(parents=True)
        items: list[dict[str, Any]] = []
        manifest_products: list[dict[str, str]] = []
        collection_products: list[dict[str, str]] = []
        for index in range(1, 4):
            source_url = (
                f"https://www.xiaohongshu.com/goods/{account_user_id}-{index}"
            )
            product_dir = f"{index:02d}_product-{index}"
            image_relative = f"{product_dir}/images/detail.png"
            image = sample_dir / image_relative
            image.parent.mkdir(parents=True)
            image.write_bytes(f"sample-image-{account_user_id}-{index}".encode())
            image_sha = hashlib.sha256(image.read_bytes()).hexdigest()
            (sample_dir / product_dir / "detail.json").write_text(
                json.dumps(
                    {
                        "link": source_url,
                        "title": f"Product {account_user_id} {index}",
                        "image_manifest": [
                            {"file": "images/detail.png", "sha256": image_sha}
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            items.append({
                "id": f"product-{account_user_id}-{index}",
                "kind": "shop_product",
                "source_url": source_url,
                "raw_evidence": {"title": f"Product {account_user_id} {index}"},
                "data": {"title": f"Product {account_user_id} {index}"},
            })
            manifest_products.append({
                "source_url": source_url,
                "image": image_relative,
                "sha256": image_sha,
            })
            collection_products.append({
                "source_url": source_url,
                "product_dir": product_dir,
            })
        manifest_path = sample_dir / "manifest.json"
        collection_path = sample_dir / "collection.json"
        manifest_path.write_text(
            json.dumps({"products": manifest_products}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        collection_path.write_text(
            json.dumps(
                {"unique_product_link_count": 3, "products": collection_products},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        result = {
            "job_id": job.id,
            "status": "succeeded",
            "detail": None,
            "selector_profile_version": "xhs-shop-v1",
            "expected_count": 3,
            "discovered_count": 3,
            "collected_count": 3,
            "raw_observation_count": 3,
            "duplicate_observation_count": 0,
            "succeeded_count": 3,
            "missing_count": 0,
            "missing_items": [],
            "collection_missing_count": 0,
            "collection_missing_items": [],
            "rejected_count": 0,
            "rejected_items": [],
            "overflow_count": 0,
            "evidence_artifacts": [],
            "items": items,
            "verification": {
                "expected_count": 3,
                "discovered_count": 3,
                "succeeded_count": 3,
                "missing_count": 0,
                "missing_items": [],
                "overflow_count": 0,
                "issues": [],
                "complete": True,
            },
            "complete": False,
            "collection_mode": "evidence_sample",
            "product_sample_limit": 3,
            "available_count_observed": 18,
            "test_override": False,
            "test_override_reason": None,
            "sample_complete": True,
            "shop_complete": False,
            "scope_classification": None,
            "scope_reason": None,
            "sample_manifest_path": manifest_path.relative_to(
                fixture.runtime_dir
            ).as_posix(),
            "sample_manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "sample_collection_path": collection_path.relative_to(
                fixture.runtime_dir
            ).as_posix(),
            "sample_collection_sha256": hashlib.sha256(
                collection_path.read_bytes()
            ).hexdigest(),
        }
        relative_path = f"evidence/shops/{job.id}/result.json"
        target = fixture.runtime_dir / relative_path
        target.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        artifact = JobArtifactRecord(
            job_id=job.id,
            kind="shop_collection_result",
            producer="android_shop_worker_v1",
            path=relative_path,
            metadata_json={"result": result},
            created_at=now,
        )
        session.add(artifact)
        session.commit()
        return f"artifact:{artifact.id}"


def _cross_account_output(
    accounts: list[tuple[str, str, str]],
) -> dict[str, Any]:
    evidence_ids = [
        evidence_id
        for _account_id, shop_id, note_id in accounts
        for evidence_id in (shop_id, note_id)
    ]
    return {
        "claims": [{
            "claim": "Multiple independent accounts expose the same demand.",
            "evidence_ids": evidence_ids,
        }],
        "product_clusters": [{
            "name": "Shared demand cluster",
            "summary": "The products and public notes share one demand.",
            "evidence_ids": evidence_ids,
        }],
        "account_demand_profiles": [
            {
                "account_user_id": account_id,
                "primary_offering": "Compact camping storage guide",
                "target_user": "Campers with limited packing space",
                "core_purchase_motivation": "Keep camping equipment organized in a small vehicle",
                "delivery_format": "Digital packing guide",
                "usage_scenarios": ["Packing for a weekend camping trip"],
                "evidence_ids": [shop_id, note_id],
            }
            for account_id, shop_id, note_id in accounts
        ],
        "cross_account_conclusion": {
            "has_specific_shared_demand": True,
            "common_demand": "Organize camping equipment in limited vehicle space",
            "commonalities": [
                "Every account targets campers who need compact, organized packing."
            ],
            "key_differences": ["The guides use different packing layouts."],
            "rationale": (
                "The accounts independently address the same user, problem and usage scenario."
            ),
            "evidence_ids": evidence_ids,
        },
        "opportunities": [{
            "title": "Cross-account demand",
            "status": "观察中",
            "summary": "Independent evidence supports human review.",
            "evidence_ids": evidence_ids,
            "next_action": "Human review only",
            "supporting_accounts": [
                {
                    "account_user_id": account_id,
                    "shop_evidence_ids": [shop_id],
                    "note_evidence_ids": [note_id],
                }
                for account_id, shop_id, note_id in accounts
            ],
        }],
    }


def _unrelated_cross_account_output(
    accounts: list[tuple[str, str, str]],
) -> dict[str, Any]:
    first, second = accounts
    all_evidence_ids = [
        evidence_id
        for _account_id, shop_id, note_id in accounts
        for evidence_id in (shop_id, note_id)
    ]
    return {
        "claims": [
            {
                "claim": "One account serves home organization buyers.",
                "evidence_ids": [first[1], first[2]],
            },
            {
                "claim": "The other account serves professional exam candidates.",
                "evidence_ids": [second[1], second[2]],
            },
        ],
        "product_clusters": [
            {
                "name": "Home organization templates",
                "summary": "Downloadable household organization checklists.",
                "evidence_ids": [first[1], first[2]],
            },
            {
                "name": "Professional exam lessons",
                "summary": "Recorded lessons and practice questions for certification exams.",
                "evidence_ids": [second[1], second[2]],
            },
        ],
        "account_demand_profiles": [
            {
                "account_user_id": first[0],
                "primary_offering": "Downloadable household organization checklists",
                "target_user": "People organizing a new home",
                "core_purchase_motivation": "Reduce clutter and remember household tasks",
                "delivery_format": "Digital checklist templates",
                "usage_scenarios": ["Moving into a new home", "Weekly household planning"],
                "evidence_ids": [first[1], first[2]],
            },
            {
                "account_user_id": second[0],
                "primary_offering": "Professional certification exam lessons",
                "target_user": "Candidates preparing for a professional exam",
                "core_purchase_motivation": "Pass a time-bounded certification exam",
                "delivery_format": "Recorded lessons and practice questions",
                "usage_scenarios": ["Exam preparation", "Practice before a test date"],
                "evidence_ids": [second[1], second[2]],
            },
        ],
        "cross_account_conclusion": {
            "has_specific_shared_demand": False,
            "common_demand": None,
            "commonalities": ["Both sell digital products"],
            "key_differences": [
                "The target users, purchase motivations, use scenarios and outcomes are unrelated."
            ],
            "rationale": (
                "A shared delivery medium is not evidence that both accounts validate one "
                "specific market demand."
            ),
            "evidence_ids": all_evidence_ids,
        },
        "opportunities": [],
    }


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
                db_module._XHS_ACCOUNT_SNAPSHOT_IMMUTABILITY_TRIGGERS[trigger_name]
            ))


@contextmanager
def _historical_snapshot_membership_delete(fixture: _Fixture):
    trigger_name = "ck_xhs_snapshot_note_immutable_delete"
    with fixture.database.engine.begin() as connection:
        connection.execute(text(f"DROP TRIGGER {trigger_name}"))
    try:
        yield
    finally:
        with fixture.database.engine.begin() as connection:
            connection.execute(text(
                db_module._XHS_ACCOUNT_SNAPSHOT_IMMUTABILITY_TRIGGERS[trigger_name]
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
    _, second_note_id = fixture.collect("u2")
    model = _ModelSpy()

    created = fixture.analysis(model).create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_ids=["u1", "u2"],
            evidence_ids=[
                f"account-note:{note_id}",
                f"account-note:{second_note_id}",
            ],
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
    with _historical_snapshot_membership_delete(fixture):
        with fixture.database.session() as session:
            session.execute(text(
                "DELETE FROM xhs_account_snapshot_notes "
                "WHERE note_record_id=:note_id"
            ), {"note_id": note_id})
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
    historical_model = _ModelSpy()
    service = fixture.analysis(historical_model)
    revalidated = service.create(_account_payload("u1", old_evidence_id))

    assert new_note_row_id > old_note_row_id
    assert new_evidence_id != old_evidence_id
    assert revalidated.status == "succeeded"
    assert revalidated.evidence_ids == [old_evidence_id]
    assert "old-note" in historical_model.calls[0].user_prompt
    assert "new-note" not in historical_model.calls[0].user_prompt
    assert [row.evidence_id for row in service.list_evidence(account_user_id="u1")] == [
        new_evidence_id
    ]
    assert service.get(historical.id).evidence_ids == [old_evidence_id]


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
    assert "old-note" in model.calls[0].user_prompt
    assert "new-note" not in model.calls[0].user_prompt
    resolved_again = service.create(_account_payload("u1", old_evidence_id))
    assert resolved_again.status == "succeeded"
    assert [row.evidence_id for row in service.list_evidence(account_user_id="u1")] == [
        f"account-note:{new_note_row_id}"
    ]


def test_artifact_drift_while_model_is_running_cannot_commit_success(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    job_id, note_row_id = fixture.collect("u1")
    evidence_id = f"account-note:{note_row_id}"
    entered_model = Event()
    release_model = Event()

    class BlockingModel(_ModelSpy):
        def generate_structured(
            self, request: StructuredModelRequest, schema: object
        ) -> ModelResult:
            entered_model.set()
            assert release_model.wait(2)
            return super().generate_structured(request, schema)

    service = fixture.analysis(BlockingModel())
    result: dict[str, object] = {}

    def create_analysis() -> None:
        result["analysis"] = service.create(_account_payload("u1", evidence_id))

    thread = Thread(target=create_analysis)
    thread.start()
    assert entered_model.wait(1)
    artifact = _artifact_for_job(fixture, job_id)
    (fixture.runtime_dir / artifact.path).write_bytes(b"tampered-during-model")
    release_model.set()
    thread.join(2)

    assert not thread.is_alive()
    created = result["analysis"]
    assert getattr(created, "status") == "needs_human"
    assert getattr(created, "error_category") == "evidence_changed_after_model"
    assert getattr(created, "output") is None
    assert service.list_opportunities() == []


def test_note_row_drift_while_model_is_running_cannot_commit_success(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    _, note_row_id = fixture.collect("u1")
    evidence_id = f"account-note:{note_row_id}"
    entered_model = Event()
    release_model = Event()

    class BlockingModel(_ModelSpy):
        def generate_structured(
            self, request: StructuredModelRequest, schema: object
        ) -> ModelResult:
            entered_model.set()
            assert release_model.wait(2)
            return super().generate_structured(request, schema)

    service = fixture.analysis(BlockingModel())
    result: dict[str, object] = {}

    def create_analysis() -> None:
        result["analysis"] = service.create(_account_payload("u1", evidence_id))

    thread = Thread(target=create_analysis)
    thread.start()
    assert entered_model.wait(1)
    with _historical_note_fact_tamper(fixture):
        with fixture.database.engine.begin() as connection:
            connection.execute(text(
                "UPDATE xhs_account_notes SET title='tampered-during-model' "
                "WHERE id=:note_id"
            ), {"note_id": note_row_id})
        release_model.set()
        thread.join(2)

    assert not thread.is_alive()
    created = result["analysis"]
    assert getattr(created, "status") == "needs_human"
    assert getattr(created, "error_category") == "evidence_changed_after_model"
    assert getattr(created, "output") is None
    assert service.list_opportunities() == []


def test_account_artifact_swap_after_final_resolve_cannot_commit_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _Fixture(tmp_path)
    job_id, note_row_id = fixture.collect("u1")
    evidence_id = f"account-note:{note_row_id}"
    artifact = _artifact_for_job(fixture, job_id)
    target = fixture.runtime_dir / artifact.path
    replacement = target.with_name(f"{target.stem}-replacement.json")
    replacement.write_bytes(b"tampered-after-final-resolve")
    service = fixture.analysis(_ModelSpy())
    real_resolve = service._resolve_evidence_in_session
    resolve_count = 0

    def swap_after_final_resolve(session: Any, payload: AnalysisCreate):
        nonlocal resolve_count
        resolved = real_resolve(session, payload)
        resolve_count += 1
        if resolve_count == 2:
            replacement.replace(target)
        return resolved

    monkeypatch.setattr(
        service,
        "_resolve_evidence_in_session",
        swap_after_final_resolve,
    )

    created = service.create(_account_payload("u1", evidence_id))

    assert resolve_count == 2
    assert target.read_bytes() == b"tampered-after-final-resolve"
    assert created.status == "needs_human"
    assert created.error_category == "evidence_changed_after_model"
    assert created.output is None
    assert service.list_opportunities() == []


@pytest.mark.parametrize("poison_id", [0, -1])
def test_noncanonical_persisted_note_id_is_never_discovered_or_used(
    tmp_path: Path,
    poison_id: int,
) -> None:
    fixture = _Fixture(tmp_path)
    _, note_row_id = fixture.collect("u1")
    with _historical_snapshot_membership_delete(fixture):
        with _historical_note_fact_tamper(fixture):
            with fixture.database.engine.begin() as connection:
                connection.execute(text("PRAGMA ignore_check_constraints=ON"))
                connection.execute(text(
                    "DELETE FROM xhs_account_snapshot_notes "
                    "WHERE note_record_id=:note_id"
                ), {"note_id": note_row_id})
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


def test_unrelated_grounded_accounts_persist_reasoned_no_opportunity(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    accounts: list[tuple[str, str, str]] = []
    for account_id in ("home-organizer", "exam-tutor"):
        _job_id, note_row_id = fixture.collect(account_id)
        accounts.append((
            account_id,
            _complete_shop_artifact(fixture, account_id),
            f"account-note:{note_row_id}",
        ))
    model = _ModelSpy(_unrelated_cross_account_output(accounts))
    service = fixture.analysis(model)

    created = service.create(AnalysisCreate(
        analysis_type="account_opportunity",
        account_user_ids=[item[0] for item in accounts],
        evidence_ids=[
            evidence_id
            for _account_id, shop_id, note_id in accounts
            for evidence_id in (shop_id, note_id)
        ],
    ))

    assert created.status == "succeeded"
    assert created.output is not None
    assert created.output.cross_account_conclusion is not None
    assert created.output.cross_account_conclusion.has_specific_shared_demand is False
    assert len(created.output.account_demand_profiles) == 2
    assert created.output.opportunities == []
    assert service.list_opportunities() == []


def test_shared_demand_conclusion_must_cite_every_requested_account(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    accounts: list[tuple[str, str, str]] = []
    for account_id in ("u1", "u2"):
        _job_id, note_row_id = fixture.collect(account_id)
        accounts.append((
            account_id,
            _complete_shop_artifact(fixture, account_id),
            f"account-note:{note_row_id}",
        ))
    output = _cross_account_output(accounts)
    output["cross_account_conclusion"]["evidence_ids"] = [
        accounts[0][1],
        accounts[0][2],
    ]
    service = fixture.analysis(_ModelSpy(output))

    created = service.create(AnalysisCreate(
        analysis_type="account_opportunity",
        account_user_ids=[item[0] for item in accounts],
        evidence_ids=[
            evidence_id
            for _account_id, shop_id, note_id in accounts
            for evidence_id in (shop_id, note_id)
        ],
    ))

    assert created.status == "failed"
    assert created.error_category == "evidence_grounding_failed"
    assert service.list_opportunities() == []


def test_opportunity_without_positive_specific_demand_cannot_be_approved(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    accounts: list[tuple[str, str, str]] = []
    for account_id in ("home-organizer", "exam-tutor"):
        _job_id, note_row_id = fixture.collect(account_id)
        accounts.append((
            account_id,
            _complete_shop_artifact(fixture, account_id),
            f"account-note:{note_row_id}",
        ))
    service = fixture.analysis(_ModelSpy(_unrelated_cross_account_output(accounts)))
    evidence_ids = [
        evidence_id
        for _account_id, shop_id, note_id in accounts
        for evidence_id in (shop_id, note_id)
    ]
    created = service.create(AnalysisCreate(
        analysis_type="account_opportunity",
        account_user_ids=[item[0] for item in accounts],
        evidence_ids=evidence_ids,
    ))
    assert created.status == "succeeded"
    now = datetime.now(UTC).replace(tzinfo=None)
    with fixture.database.session() as session:
        record = OpportunityRecord(
            analysis_id=created.id,
            title="Legacy unsupported candidate",
            status="升温",
            summary="Persisted before the specific-demand approval gate.",
            evidence_ids_json=evidence_ids,
            review_status="pending_review",
            evidence_level="warming_candidate",
            supporting_accounts_json=[],
            supporting_products_json=[],
            supporting_notes_json=[],
            reviewed_at=None,
            rejection_reason=None,
            next_action="human review",
            created_at=now,
        )
        session.add(record)
        session.commit()
        opportunity_id = record.id

    with pytest.raises(OpportunityStateError):
        service.review_opportunity(
            opportunity_id,
            OpportunityReviewCreate(decision="approve"),
        )
    rejected = service.review_opportunity(
        opportunity_id,
        OpportunityReviewCreate(decision="reject", reason="No shared demand"),
    )
    assert rejected.review_status == "rejected"


@pytest.mark.parametrize(
    ("account_ids", "expected_level"),
    [
        (["u1", "u2"], "warming_candidate"),
        (["u1", "u2", "u3"], "validated_candidate"),
    ],
)
def test_cross_account_opportunity_level_and_review_are_server_derived(
    tmp_path: Path,
    account_ids: list[str],
    expected_level: str,
) -> None:
    fixture = _Fixture(tmp_path)
    accounts: list[tuple[str, str, str]] = []
    for account_id in account_ids:
        _job_id, note_row_id = fixture.collect(account_id)
        shop_id = _complete_shop_artifact(fixture, account_id)
        accounts.append((account_id, shop_id, f"account-note:{note_row_id}"))
    model = _ModelSpy(_cross_account_output(accounts))
    service = fixture.analysis(model)
    evidence_ids = [
        evidence_id
        for _account_id, shop_id, note_id in accounts
        for evidence_id in (shop_id, note_id)
    ]

    created = service.create(AnalysisCreate(
        analysis_type="account_opportunity",
        account_user_ids=account_ids,
        evidence_ids=evidence_ids,
    ))

    assert created.status == "succeeded"
    [opportunity] = service.list_opportunities()
    assert opportunity.review_status == "pending_review"
    assert opportunity.evidence_level == expected_level
    assert opportunity.supporting_account_count == len(account_ids)
    assert {item.account_user_id for item in opportunity.supporting_accounts} == set(account_ids)
    assert len(opportunity.supporting_products) == len(account_ids)
    assert all(item.image_evidence_count == 1 for item in opportunity.supporting_products)
    assert len(opportunity.supporting_notes) == len(account_ids)
    approved = service.review_opportunity(
        opportunity.id,
        OpportunityReviewCreate(decision="approve"),
    )
    assert approved.review_status == "approved"
    with pytest.raises(OpportunityStateError):
        service.review_opportunity(
            opportunity.id,
            OpportunityReviewCreate(decision="reject", reason="late change"),
        )


def test_three_product_evidence_samples_can_support_a_warming_candidate(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    accounts: list[tuple[str, str, str]] = []
    for account_id in ("u1", "u2"):
        _job_id, note_row_id = fixture.collect(account_id)
        accounts.append((
            account_id,
            _evidence_sample_shop_artifact(fixture, account_id),
            f"account-note:{note_row_id}",
        ))
    service = fixture.analysis(_ModelSpy(_cross_account_output(accounts)))

    created = service.create(AnalysisCreate(
        analysis_type="account_opportunity",
        account_user_ids=["u1", "u2"],
        evidence_ids=[
            evidence_id
            for _account_id, shop_id, note_id in accounts
            for evidence_id in (shop_id, note_id)
        ],
    ))

    assert created.status == "succeeded"
    [opportunity] = service.list_opportunities()
    assert opportunity.evidence_level == "warming_candidate"
    assert opportunity.review_status == "pending_review"
    assert opportunity.supporting_account_count == 2
    assert len(opportunity.supporting_products) == 6


def test_evidence_sample_requires_a_real_hash_bound_in_scope_gate(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    shop_id = _evidence_sample_shop_artifact(fixture, "u1")
    artifact_id = int(shop_id.removeprefix("artifact:"))
    with fixture.database.session() as session:
        artifact = session.get(JobArtifactRecord, artifact_id)
        assert artifact is not None
        job = session.get(JobRecord, artifact.job_id)
        assert job is not None
        job.input_data = {**job.input_data, "scope_gate_job_id": "missing-gate"}
        session.commit()
    model = _ModelSpy()

    created = fixture.analysis(model).create(AnalysisCreate(
        analysis_type="account_report",
        account_user_id="u1",
        evidence_ids=[shop_id],
    ))

    assert created.status == "needs_human"
    assert created.error_category == "deep_verification_incomplete"
    assert model.calls == []


def test_evidence_sample_rejects_tampered_product_image_before_model(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    shop_id = _evidence_sample_shop_artifact(fixture, "u1")
    [image, *_] = sorted(
        fixture.runtime_dir.glob("evidence/shops/*/sample-products/*/images/detail.png")
    )
    image.write_bytes(b"tampered-image")
    model = _ModelSpy()

    created = fixture.analysis(model).create(AnalysisCreate(
        analysis_type="account_report",
        account_user_id="u1",
        evidence_ids=[shop_id],
    ))

    assert created.status == "needs_human"
    assert created.error_category == "deep_verification_incomplete"
    assert model.calls == []


def test_cross_account_support_must_match_each_evidence_owner(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    accounts: list[tuple[str, str, str]] = []
    for account_id in ("u1", "u2"):
        _job_id, note_row_id = fixture.collect(account_id)
        accounts.append((
            account_id,
            _complete_shop_artifact(fixture, account_id),
            f"account-note:{note_row_id}",
        ))
    output = _cross_account_output(accounts)
    output["opportunities"][0]["supporting_accounts"][0]["note_evidence_ids"] = [
        accounts[1][2]
    ]
    service = fixture.analysis(_ModelSpy(output))

    created = service.create(AnalysisCreate(
        analysis_type="product_cluster",
        account_user_ids=["u1", "u2"],
        evidence_ids=[
            evidence_id
            for _account_id, shop_id, note_id in accounts
            for evidence_id in (shop_id, note_id)
        ],
    ))

    assert created.status == "failed"
    assert created.error_category == "evidence_grounding_failed"
    assert service.list_opportunities() == []
