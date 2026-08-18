import json
import hashlib
import subprocess
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import pytest
from sqlalchemy import select

from backend.app.adapters.contracts import CollectionItem, CollectionRequest, CollectionResult
from backend.app.adapters.xhs_cli_read import XhsCliReadAdapter
from backend.app.db import Database
from backend.app.features.xhs.models import XhsAccountNoteRecord, XhsAccountProfileRecord
import backend.app.features.xhs.service as xhs_service_module
from backend.app.features.xhs.service import CollectionServiceClosed, XhsCollectionService
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState
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


def _prepared_cli_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "runtime" / "xhs-cli-state"
    config_dir = state_dir / ".xhs-cli"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cookies.json").write_text(
        json.dumps({"cookies": {"a1": "prepared-a1", "web_session": "prepared-session"}}),
        encoding="utf-8",
    )
    return state_dir


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

    adapter = XhsCliReadAdapter(
        executable="xhs",
        state_dir=_prepared_cli_state(tmp_path),
        timeout_seconds=0.01,
        runner=timed_out,
    )
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


def test_structured_header_credentials_are_redacted_before_artifact_and_facts(tmp_path: Path) -> None:
    secret = "structured-secret-sentinel"
    ordinary = "ordinary-value-sentinel"

    class LeakyAdapter(_Adapter):
        def fetch_account(self, request: CollectionRequest) -> CollectionResult:
            result = super().fetch_account(request)
            result.items[0].raw_evidence["transport"] = {"headers": [
                {"name": "Cookie", "value": secret},
                {"name": "token", "value": secret},
                {"name": "title", "value": ordinary},
            ]}
            return result

    service = _service(tmp_path, LeakyAdapter(), submitter=lambda *_args: None)
    queued = service.submit_account("user-1", 1)

    completed = service.execute(queued.id)

    assert completed is not None and completed.state is JobState.succeeded
    artifact_text = (service.runtime_dir / completed.artifacts[0].path).read_text(encoding="utf-8")
    assert secret not in artifact_text
    assert ordinary in artifact_text
    with service.database.session() as session:
        profile = session.get(XhsAccountProfileRecord, "user-1")
        rendered = json.dumps(profile.raw_evidence)
        assert secret not in rendered
        assert ordinary in rendered
    service.database.close()


def test_real_adapter_account_alias_credentials_never_reach_artifact_or_facts(
    tmp_path: Path,
) -> None:
    secrets = [
        f"real-account-alias-secret-sentinel-{index:02d}-end"
        for index in range(40)
    ]
    responses = iter([
        subprocess.CompletedProcess(
            ["xhs"], 0,
            stdout=json.dumps({"user": {
                "id": "user-1",
                "nickname": "Alice",
                "accessToken": secrets[0],
                "csrfToken": secrets[1],
                "bearerToken": secrets[2],
                "apiToken": secrets[3],
                "oauthToken": secrets[4],
                "sessionId": secrets[5],
                "web_session": secrets[6],
                "personalAccessToken": secrets[7],
                "OAuthToken": secrets[20],
                "XOAuthToken": secrets[21],
                "XCSRFToken": secrets[22],
                "XAPIKey": secrets[23],
                "URLToken": secrets[24],
                "AccessToken": secrets[25],
                "SessionID": secrets[26],
                "WebSession": secrets[27],
                "headers": [
                    {"name": "xsrfToken", "value": secrets[8]},
                    {"name": "jwtToken", "value": secrets[9]},
                    {"name": "x-api-token", "value": secrets[10]},
                    {"name": "x-oauth-token", "value": secrets[11]},
                    {"name": "x-session-id", "value": secrets[12]},
                ],
            }}).encode(),
            stderr=b"",
        ),
        subprocess.CompletedProcess(
            ["xhs"], 0,
            stdout=json.dumps({"notes": [{
                "id": "note-1",
                "user_id": "user-1",
                "refreshToken": secrets[13],
                "xsrfToken": secrets[14],
                "jwtToken": secrets[15],
                "apiToken": secrets[16],
                "OAuthToken": secrets[28],
                "XOAuthToken": secrets[29],
                "XCSRFToken": secrets[30],
                "XAPIKey": secrets[31],
                "APIKey": secrets[32],
                "CSRFToken": secrets[33],
                "JWTToken": secrets[34],
                "URLToken": secrets[35],
                "AccessToken": secrets[36],
                "RefreshToken": secrets[37],
                "SessionID": secrets[38],
                "WebSession": secrets[39],
                "headers": [
                    {"name": "csrfToken", "value": secrets[17]},
                    {"name": "bearerToken", "value": secrets[18]},
                    {"name": "personalAccessToken", "value": secrets[19]},
                ],
            }]}).encode(),
            stderr=b"",
        ),
    ])

    def runner(_argv, **_kwargs):
        return next(responses)

    adapter = XhsCliReadAdapter(
        executable="xhs",
        state_dir=_prepared_cli_state(tmp_path),
        runner=runner,
    )
    service = _service(tmp_path, adapter, submitter=lambda *_args: None)
    queued = service.submit_account("user-1", 1)

    completed = service.execute(queued.id)

    assert completed is not None and completed.state is JobState.succeeded
    artifact_text = (
        service.runtime_dir / completed.artifacts[0].path
    ).read_text(encoding="utf-8")
    with service.database.session() as session:
        profile = session.get(XhsAccountProfileRecord, "user-1")
        note = session.scalar(select(XhsAccountNoteRecord))
        profile_evidence = json.dumps(profile.raw_evidence)
        note_evidence = json.dumps(note.raw_evidence)
        for secret in secrets:
            assert secret not in artifact_text
            assert secret not in profile_evidence
            assert secret not in note_evidence
    service.database.close()


def test_real_adapter_search_alias_credentials_never_reach_artifact_or_read_facts(
    tmp_path: Path,
) -> None:
    secrets = [
        f"real-search-alias-secret-sentinel-{index:02d}-end"
        for index in range(36)
    ]
    response = subprocess.CompletedProcess(
        ["xhs"], 0,
        stdout=json.dumps([{
            "id": "note-1",
            "auth_token": secrets[0],
            "csrfToken": secrets[1],
            "xsrfToken": secrets[2],
            "bearerToken": secrets[3],
            "jwtToken": secrets[4],
            "apiToken": secrets[5],
            "oauthToken": secrets[6],
            "sessionId": secrets[7],
            "web_session": secrets[8],
            "personalAccessToken": secrets[9],
            "OAuthToken": secrets[18],
            "XOAuthToken": secrets[19],
            "XCSRFToken": secrets[20],
            "XAPIKey": secrets[21],
            "URLToken": secrets[22],
            "APIKey": secrets[23],
            "CSRFToken": secrets[24],
            "JWTToken": secrets[25],
            "AccessToken": secrets[26],
            "RefreshToken": secrets[27],
            "SessionID": secrets[28],
            "WebSession": secrets[29],
            "headers": [
                {"name": "cookie_string", "value": secrets[10]},
                {"name": "csrfToken", "value": secrets[11]},
                {"name": "x-api-token", "value": secrets[12]},
                {"name": "x-oauth-token", "value": secrets[13]},
                {"name": "x-session-id", "value": secrets[14]},
                {"name": "xsrfToken", "value": secrets[15]},
                {"name": "bearerToken", "value": secrets[16]},
                {"name": "jwtToken", "value": secrets[17]},
            ],
            "Headers": [
                {"Name": "Cookie", "Value": secrets[30]},
                {"NAME": "Authorization", "VALUE": secrets[31]},
            ],
            "Cookies": {"arbitrary_cookie_name": secrets[32]},
            "tokens": {"provider_specific_name": secrets[33]},
            "xsecToken": secrets[34],
            "xsec_token": secrets[35],
            "noteCard": {
                "displayTitle": "收纳",
                "user": {"userId": "user-1"},
            },
        }]).encode(),
        stderr=b"",
    )
    adapter = XhsCliReadAdapter(
        executable="xhs",
        state_dir=_prepared_cli_state(tmp_path),
        runner=lambda _argv, **_kwargs: response,
    )
    service = _service(tmp_path, adapter, submitter=lambda *_args: None)
    queued = service.submit_search("收纳", 1)

    completed = service.execute(queued.id)
    facts = service.get_search_results(queued.id)

    assert completed is not None and completed.state is JobState.succeeded
    artifact_text = (
        service.runtime_dir / completed.artifacts[0].path
    ).read_text(encoding="utf-8")
    rendered_facts = facts.model_dump_json()
    for secret in secrets:
        assert secret not in artifact_text
        assert secret not in rendered_facts
    service.database.close()


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


def test_commit_ack_and_transient_read_failure_never_overwrite_succeeded_artifact(tmp_path: Path) -> None:
    service = _service(tmp_path, _Adapter(), submitter=lambda *_args: None)
    queued = service.submit_account("user-1", 1)
    original_session = service.database.session
    original_get = service.job_service.get
    ack_lost = False
    remaining_read_failures = 2

    class CommitAckProxy:
        def __init__(self, session):
            self._session = session

        def __getattr__(self, name):
            return getattr(self._session, name)

        def commit(self):
            nonlocal ack_lost
            final_state = self._session.get(JobRecord, queued.id).state
            self._session.commit()
            if not ack_lost and JobState(final_state) is JobState.succeeded:
                ack_lost = True
                raise RuntimeError("commit acknowledgement lost")

    @contextmanager
    def uncertain_session():
        with original_session() as session:
            yield CommitAckProxy(session)

    def transient_get(job_id: str):
        nonlocal remaining_read_failures
        if ack_lost and remaining_read_failures:
            remaining_read_failures -= 1
            raise RuntimeError("fresh read temporarily unavailable")
        return original_get(job_id)

    service.database.session = uncertain_session
    service.job_service.get = transient_get

    completed = service.execute(queued.id)
    durable = original_get(queued.id)
    artifact_path = service.runtime_dir / durable.artifacts[0].path

    assert completed is not None and completed.state is JobState.succeeded
    assert durable.state is JobState.succeeded
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == durable.artifacts[0].metadata["sha256"]
    service.database.close()


def test_shutdown_fence_wins_before_final_commit_and_rolls_back_account_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, _Adapter(), submitter=lambda *_args: None)
    queued = service.submit_account("user-1", 1)
    persisted, release = Event(), Event()
    original_persist = xhs_service_module.persist_exact_account_result

    def paused_persist(*args, **kwargs):
        value = original_persist(*args, **kwargs)
        persisted.set()
        assert release.wait(10)
        return value

    monkeypatch.setattr(xhs_service_module, "persist_exact_account_result", paused_persist)
    execution: list[object] = []
    worker = Thread(target=lambda: execution.append(service.execute(queued.id)))
    worker.start()
    assert persisted.wait(2)
    close_result: list[bool] = []
    closer = Thread(target=lambda: close_result.append(service.close()))
    closer.start()
    deadline = monotonic() + 2
    while not service._shutdown_requested() and monotonic() < deadline:
        sleep(0.01)
    assert service._shutdown_requested()
    release.set()
    worker.join(10)
    closer.join(10)

    assert not worker.is_alive() and not closer.is_alive()
    assert close_result == [True]
    assert service.job_service.get(queued.id).state is JobState.cancelled
    with service.database.session() as session:
        assert session.scalars(select(XhsAccountProfileRecord)).all() == []
        assert session.scalars(select(XhsAccountNoteRecord)).all() == []
    service.database.close()


def test_submit_does_not_hold_lifecycle_lock_while_waiting_for_finalizer_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, _Adapter(), submitter=lambda *_args: None)
    first = service.submit_account("user-1", 1)
    facts_flushed, release_finalizer = Event(), Event()
    submit_entered_db, finalizer_done, submit_done = Event(), Event(), Event()
    original_persist = xhs_service_module.persist_exact_account_result
    original_create = service.job_service.create
    outcomes: list[object] = []
    errors: list[BaseException] = []

    def paused_persist(*args, **kwargs):
        value = original_persist(*args, **kwargs)
        facts_flushed.set()
        assert release_finalizer.wait(10)
        return value

    def observed_create(**kwargs):
        submit_entered_db.set()
        return original_create(**kwargs)

    monkeypatch.setattr(xhs_service_module, "persist_exact_account_result", paused_persist)
    monkeypatch.setattr(service.job_service, "create", observed_create)

    def finalize() -> None:
        try:
            outcomes.append(service.execute(first.id))
        except BaseException as error:
            errors.append(error)
        finally:
            finalizer_done.set()

    def submit() -> None:
        try:
            outcomes.append(service.submit_search("并发", 1))
        except BaseException as error:
            errors.append(error)
        finally:
            submit_done.set()

    finalizer_thread = Thread(target=finalize)
    finalizer_thread.start()
    assert facts_flushed.wait(2)
    submit_thread = Thread(target=submit)
    submit_thread.start()
    assert submit_entered_db.wait(2)
    release_finalizer.set()
    finalized_without_lock_inversion = finalizer_done.wait(1)
    submit_done.wait(7)
    finalizer_thread.join(1)
    submit_thread.join(1)
    service.close()
    service.database.close()

    assert finalized_without_lock_inversion
    assert not finalizer_thread.is_alive() and not submit_thread.is_alive()
    assert errors == []
    assert len(outcomes) == 2


def test_close_fences_a_submit_blocked_in_database_without_waiting_on_lifecycle_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, _Adapter(), submitter=lambda *_args: None)
    create_entered, release_create, close_done = Event(), Event(), Event()
    original_create = service.job_service.create
    submitted: list[object] = []
    submit_errors: list[BaseException] = []

    def paused_create(**kwargs):
        create_entered.set()
        assert release_create.wait(10)
        return original_create(**kwargs)

    monkeypatch.setattr(service.job_service, "create", paused_create)

    def submit() -> None:
        try:
            submitted.append(service.submit_account("user-1", 1))
        except BaseException as error:
            submit_errors.append(error)

    submit_thread = Thread(target=submit)
    submit_thread.start()
    assert create_entered.wait(2)
    close_thread = Thread(target=lambda: (service.close(), close_done.set()))
    close_thread.start()
    deadline = monotonic() + 1
    while service._accepting and monotonic() < deadline:
        sleep(0.01)
    fenced_before_database_release = not service._accepting
    release_create.set()
    submit_thread.join(7)
    close_thread.join(7)
    jobs = service.job_service.list()
    service.database.close()

    assert fenced_before_database_release
    assert close_done.is_set()
    assert submitted == []
    assert len(submit_errors) == 1
    assert isinstance(submit_errors[0], CollectionServiceClosed)
    assert jobs and all(job.state is JobState.cancelled for job in jobs)


def test_two_normal_submits_complete_without_lifecycle_or_database_lock_contention(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _Adapter(), submitter=lambda *_args: None)
    start = Event()
    jobs: list[object] = []
    errors: list[BaseException] = []

    def submit(index: int) -> None:
        assert start.wait(2)
        try:
            jobs.append(service.submit_search(f"并发-{index}", 1))
        except BaseException as error:
            errors.append(error)

    threads = [Thread(target=submit, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(3)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(jobs) == 2
    assert all(service.job_service.get(job.id).state is JobState.queued for job in jobs)
    service.close()
    service.database.close()


def test_conflicting_search_owner_aliases_cannot_finalize_success(tmp_path: Path) -> None:
    class ConflictingAdapter(_Adapter):
        def search_notes(self, request: CollectionRequest) -> CollectionResult:
            result = super().search_notes(request)
            result.items[0].data.update({"user_id": "user-1", "userId": "user-2"})
            return result

    service = _service(tmp_path, ConflictingAdapter(), submitter=lambda *_args: None)
    queued = service.submit_search("收纳", 1)

    completed = service.execute(queued.id)

    assert completed is not None and completed.state is JobState.failed
    assert completed.artifacts[0].metadata.get("complete") is not True
    service.database.close()


def test_historical_search_artifact_with_conflicting_owner_aliases_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path, _Adapter(), submitter=lambda *_args: None)
    queued = service.submit_search("收纳", 1)
    completed = service.execute(queued.id)
    assert completed is not None
    artifact_path = service.runtime_dir / completed.artifacts[0].path
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    payload["result"]["items"][0]["data"].update({"user_id": "user-1", "userId": "user-2"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    artifact_path.write_bytes(encoded)
    with service.database.session() as session:
        artifact = session.get(JobArtifactRecord, completed.artifacts[0].metadata["artifact_id"])
        artifact.metadata_json = {
            **artifact.metadata_json,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
        }
        session.commit()

    with pytest.raises(LookupError, match="do not exist"):
        service.get_search_results(queued.id)
    service.database.close()


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
