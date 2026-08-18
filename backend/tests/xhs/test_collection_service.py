import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import select

from backend.app.adapters.contracts import CollectionItem, CollectionRequest, CollectionResult
from backend.app.adapters.xhs_cli_read import XhsCliReadAdapter
from backend.app.db import Database
from backend.app.features.xhs.models import XhsAccountNoteRecord, XhsAccountProfileRecord
from backend.app.features.xhs.service import CollectionServiceClosed, XhsCollectionService
from backend.app.models.jobs import JobRecord, JobState
from backend.app.services.jobs import JobService


def _account_result(user_id: str = "user-1") -> CollectionResult:
    return CollectionResult(
        status="succeeded",
        items=[
            CollectionItem(
                id=f"profile:{user_id}",
                kind="profile",
                source_url=f"https://www.xiaohongshu.com/user/profile/{user_id}",
                raw_evidence={"profile": {"user_id": user_id, "nickname": "Alice"}},
                data={"user_id": user_id, "nickname": "Alice"},
            ),
            CollectionItem(
                id="note:note-1", kind="note",
                source_url="https://www.xiaohongshu.com/explore/note-1",
                raw_evidence={"row": {"note_id": "note-1", "user_id": user_id}},
                data={"note_id": "note-1", "user_id": user_id, "title": "First"},
            ),
        ],
        expected_count_known=True, expected_count=2, succeeded_count=2,
        observed_count=2, overflow_count=0, complete=True,
    )


def _search_result() -> CollectionResult:
    return CollectionResult(
        status="succeeded",
        items=[CollectionItem(
            id="note:search-1", kind="note",
            source_url="https://www.xiaohongshu.com/explore/search-1",
            raw_evidence={"row": {"note_id": "search-1", "title": "收纳"}},
            data={"note_id": "search-1", "title": "收纳"},
        )],
        expected_count_known=True, expected_count=1, succeeded_count=1,
        observed_count=1, overflow_count=0, complete=True,
    )


class _Adapter:
    def __init__(self) -> None:
        self.requests: list[CollectionRequest] = []

    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        self.requests.append(request)
        return _account_result(request.parameters["user_id"])

    def search_notes(self, request: CollectionRequest) -> CollectionResult:
        self.requests.append(request)
        return _search_result()


def _service(tmp_path: Path, adapter: object, *, submitter=None) -> XhsCollectionService:
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3", runtime_dir=runtime_dir)
    jobs = JobService(database, runtime_dir=runtime_dir)
    return XhsCollectionService(
        database=database, job_service=jobs, adapter=adapter,
        runtime_dir=runtime_dir, submitter=submitter,
    )


def test_account_uses_profile_plus_n_expected_count_and_atomic_success(tmp_path: Path) -> None:
    adapter = _Adapter()
    service = _service(tmp_path, adapter, submitter=lambda *_args: None)
    queued = service.submit_account("user-1", 1)

    completed = service.execute(queued.id)

    assert adapter.requests[0].expected_count == 2
    assert completed is not None and completed.state is JobState.succeeded
    assert completed.progress_current == completed.progress_total == 1
    assert completed.artifacts[0].kind == "xhs_account_collection_raw"
    assert completed.artifacts[0].producer == "xhs_cli_read_worker_v1"
    assert completed.artifacts[0].metadata["source"] == "xhs-cli"
    assert completed.artifacts[0].metadata["user_id"] == "user-1"
    assert completed.artifacts[0].metadata["expected_note_count"] == 1
    assert completed.artifacts[0].metadata["expected_item_count"] == 2
    assert completed.artifacts[0].metadata["succeeded_note_count"] == 1
    assert completed.artifacts[0].metadata["succeeded_item_count"] == 2
    with service.database.session() as session:
        assert session.get(XhsAccountProfileRecord, "user-1") is not None
        assert [row.note_id for row in session.scalars(select(XhsAccountNoteRecord))] == ["note-1"]


def test_cancel_after_external_result_leaves_no_account_facts_or_artifact(tmp_path: Path) -> None:
    holder: dict[str, object] = {}

    class CancellingAdapter(_Adapter):
        def fetch_account(self, request: CollectionRequest) -> CollectionResult:
            service = holder["service"]
            service.job_service.transition(request.parameters["job_id"], JobState.cancelled)
            return super().fetch_account(request)

    service = _service(tmp_path, CancellingAdapter(), submitter=lambda *_args: None)
    holder["service"] = service
    queued = service.submit_account("user-1", 1)

    assert service.execute(queued.id) is None
    assert service.job_service.get(queued.id).state is JobState.cancelled
    with service.database.session() as session:
        assert session.scalars(select(XhsAccountProfileRecord)).all() == []
        assert session.scalars(select(XhsAccountNoteRecord)).all() == []
    assert service.job_service.get(queued.id).artifacts == []


def test_search_persists_its_own_raw_artifact_and_normalized_read_facts(tmp_path: Path) -> None:
    service = _service(tmp_path, _Adapter(), submitter=lambda *_args: None)
    queued = service.submit_search("收纳", 1)

    completed = service.execute(queued.id)
    results = service.get_search_results(queued.id)

    assert completed is not None and completed.state is JobState.succeeded
    assert completed.type == "xhs_note_search"
    assert completed.artifacts[0].kind == "xhs_note_search_raw"
    assert [item.note_id for item in results.items] == ["search-1"]
    with service.database.session() as session:
        assert session.scalars(select(XhsAccountProfileRecord)).all() == []


def test_tampered_search_artifact_is_not_returned_as_normalized_fact(tmp_path: Path) -> None:
    service = _service(tmp_path, _Adapter(), submitter=lambda *_args: None)
    queued = service.submit_search("收纳", 1)
    completed = service.execute(queued.id)
    assert completed is not None
    artifact_path = service.runtime_dir / completed.artifacts[0].path
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    payload["result"]["items"][0]["data"]["title"] = "tampered"
    artifact_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LookupError, match="do not exist"):
        service.get_search_results(queued.id)


def test_shutdown_rejects_admission_and_late_result_cannot_succeed(tmp_path: Path) -> None:
    started, release = Event(), Event()

    class BlockingAdapter(_Adapter):
        def fetch_account(self, request: CollectionRequest) -> CollectionResult:
            started.set()
            release.wait(2)
            return super().fetch_account(request)

    service = _service(tmp_path, BlockingAdapter())
    queued = service.submit_account("user-1", 1)
    assert started.wait(1)

    assert service.close() is False
    with pytest.raises(CollectionServiceClosed):
        service.submit_account("user-2", 1)
    release.set()
    assert service.wait_for_idle(timeout=1)

    assert service.job_service.get(queued.id).state is JobState.cancelled
    with service.database.session() as session:
        assert session.scalars(select(XhsAccountProfileRecord)).all() == []


def test_external_exception_is_persisted_without_secret_text(tmp_path: Path) -> None:
    class FailingAdapter(_Adapter):
        def fetch_account(self, request: CollectionRequest) -> CollectionResult:
            raise RuntimeError("secret-sentinel")

    service = _service(tmp_path, FailingAdapter(), submitter=lambda *_args: None)
    queued = service.submit_account("user-1", 1)

    failed = service.execute(queued.id)

    assert failed is not None and failed.state is JobState.failed
    assert failed.error_category == "xhs_collection_failed"
    assert "secret-sentinel" not in json.dumps(failed, default=str)
    assert "secret-sentinel" not in (service.runtime_dir / failed.artifacts[0].path).read_text(encoding="utf-8")


def test_malformed_adapter_result_becomes_a_sanitized_failed_fact(tmp_path: Path) -> None:
    class MalformedAdapter(_Adapter):
        def search_notes(self, request: CollectionRequest):
            return {"cookie": "secret-sentinel"}

    service = _service(tmp_path, MalformedAdapter(), submitter=lambda *_args: None)
    queued = service.submit_search("收纳", 1)

    failed = service.execute(queued.id)

    assert failed is not None and failed.state is JobState.failed
    assert failed.error_category == "xhs_collection_failed"
    assert "secret-sentinel" not in json.dumps(failed, default=str)


def test_subprocess_timeout_becomes_a_durable_failed_job(tmp_path: Path) -> None:
    def timed_out(argv, **_kwargs):
        raise subprocess.TimeoutExpired(argv, timeout=0.01)

    adapter = XhsCliReadAdapter(executable="xhs", timeout_seconds=0.01, runner=timed_out)
    service = _service(tmp_path, adapter, submitter=lambda *_args: None)
    queued = service.submit_search("收纳", 1)

    failed = service.execute(queued.id)

    assert failed is not None and failed.state is JobState.failed
    assert failed.error_category == "timeout"
    assert failed.artifacts[0].metadata["complete"] is False


def test_success_result_is_redacted_again_before_artifact_and_fact_persistence(tmp_path: Path) -> None:
    class LeakyAdapter(_Adapter):
        def fetch_account(self, request: CollectionRequest) -> CollectionResult:
            result = super().fetch_account(request)
            result.items[0].raw_evidence["cookie"] = "secret-sentinel"
            result.items[1].raw_evidence["authorization"] = "secret-sentinel"
            return result

    service = _service(tmp_path, LeakyAdapter(), submitter=lambda *_args: None)
    queued = service.submit_account("user-1", 1)

    completed = service.execute(queued.id)

    assert completed is not None and completed.state is JobState.succeeded
    assert "secret-sentinel" not in (service.runtime_dir / completed.artifacts[0].path).read_text(encoding="utf-8")
    with service.database.session() as session:
        profile = session.get(XhsAccountProfileRecord, "user-1")
        note = session.scalar(select(XhsAccountNoteRecord))
        assert "secret-sentinel" not in json.dumps(profile.raw_evidence)
        assert "secret-sentinel" not in json.dumps(note.raw_evidence)


def test_commit_acknowledgement_error_reads_back_committed_success(tmp_path: Path) -> None:
    service = _service(tmp_path, _Adapter(), submitter=lambda *_args: None)
    queued = service.submit_account("user-1", 1)
    original_session = service.database.session
    injected = False

    class CommitAckProxy:
        def __init__(self, session):
            self._session = session

        def __getattr__(self, name):
            return getattr(self._session, name)

        def commit(self):
            nonlocal injected
            final_state = self._session.get(JobRecord, queued.id).state
            self._session.commit()
            if not injected and JobState(final_state) is JobState.succeeded:
                injected = True
                raise RuntimeError("commit acknowledgement lost")

    @contextmanager
    def uncertain_session():
        with original_session() as session:
            yield CommitAckProxy(session)

    service.database.session = uncertain_session

    completed = service.execute(queued.id)

    assert injected is True
    assert completed is not None and completed.state is JobState.succeeded
    assert len(completed.artifacts) == 1
    assert service.get_profile("user-1").collection_job_id == queued.id


def test_submit_failure_leaves_a_durable_failed_reservation(tmp_path: Path) -> None:
    def reject_submit(*_args):
        raise RuntimeError("executor unavailable secret-sentinel")

    service = _service(tmp_path, _Adapter(), submitter=reject_submit)
    with pytest.raises(RuntimeError, match="executor unavailable"):
        service.submit_account("user-1", 1)

    jobs = service.job_service.list()
    assert len(jobs) == 1 and jobs[0].state is JobState.failed
    assert jobs[0].error_category == "xhs_collection_schedule_failed"
    assert "secret-sentinel" not in json.dumps(jobs[0], default=str)


def test_restart_never_implicitly_resumes_reserved_work(tmp_path: Path) -> None:
    service = _service(tmp_path, _Adapter(), submitter=lambda *_args: None)
    queued = service.submit_account("user-1", 1)
    running = service.submit_search("收纳", 1)
    service.job_service.claim(running.id)

    restarted = XhsCollectionService(
        database=service.database, job_service=service.job_service, adapter=_Adapter(),
        runtime_dir=service.runtime_dir, submitter=lambda *_args: None,
    )

    assert restarted.job_service.get(queued.id).state is JobState.needs_human
    assert restarted.job_service.get(running.id).state is JobState.needs_human
    assert restarted.job_service.get(queued.id).error_category == "worker_restart_required"
