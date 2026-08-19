from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, get_ident
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from backend.app.adapters.contracts import CollectionItem, CollectionRequest, CollectionResult
from backend.app.adapters.qianfan_playwright import (
    DEFAULT_QIANFAN_SELECTOR_PROFILE,
    QIANFAN_RANK_URL,
    QianfanPlaywrightAdapter,
    QianfanSelectorProfile,
)
from backend.app.db import Database
from backend.app.features.radar.models import BoardName, DimensionName
from backend.app.features.radar.qianfan_service import (
    QIANFAN_SCOPES,
    QianfanCollectionCreate,
    QianfanCollectionService,
)
from backend.app.models.jobs import JobRecord
from backend.app.features.radar.service import RadarService
from backend.app.main import create_app
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService
from backend.app.settings import Settings


class _ExactScopeAdapter:
    selector_profile_version = "qianfan-controlled-test-v1"

    def __init__(
        self,
        jobs: JobService,
        runtime_dir: Path,
        *,
        partial_scope: tuple[str, str] | None = None,
        artifact_source_url: str = QIANFAN_RANK_URL,
    ) -> None:
        self.jobs = jobs
        self.runtime_dir = runtime_dir
        self.partial_scope = partial_scope
        self.artifact_source_url = artifact_source_url
        self.calls: list[tuple[str, str, int]] = []
        self.thread_ids: list[int] = []

    def collect_scope(
        self,
        request: CollectionRequest,
        *,
        board: BoardName,
        dimension: DimensionName,
        selector_profile: QianfanSelectorProfile,
    ) -> CollectionResult:
        assert selector_profile.version == self.selector_profile_version
        self.calls.append((board, dimension, int(request.expected_count or 0)))
        self.thread_ids.append(get_ident())
        job_id = str(request.parameters["job_id"])
        raw = {
            "profile_version": self.selector_profile_version,
            "verified_scope": {"board": board, "dimension": dimension},
            "responses": [{"status": 200, "body": {"scope": [board, dimension]}}],
        }
        relative = Path("evidence") / "qianfan" / f"{job_id}.json"
        absolute = self.runtime_dir / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
        absolute.write_bytes(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        self.jobs.attach_artifact(
            job_id,
            kind="qianfan_raw_capture",
            path=relative.as_posix(),
            metadata={
                "sha256": digest,
                "source_url": self.artifact_source_url,
                "selector_profile_version": self.selector_profile_version,
                "board": board,
                "dimension": dimension,
            },
        )
        if self.partial_scope == (board, dimension):
            return CollectionResult(
                status="partial",
                detail="expected_items_missing",
                evidence_artifacts=[relative.as_posix()],
                items=[],
                expected_count_known=True,
                expected_count=1,
                succeeded_count=0,
                missing_items=[
                    {
                        "reference": "qianfan_ranking:1",
                        "reason": "expected_item_not_observed",
                        "raw_evidence": raw,
                    }
                ],
                overflow_count=0,
                complete=False,
            )
        item = CollectionItem(
            id=f"{board}-{dimension}-note",
            kind="rank_item",
            source_url=f"https://www.xiaohongshu.com/explore/{len(self.calls):024d}",
            raw_evidence={"item": {"userFansNum": 321}, "response_scope": [board, dimension]},
            data={
                "rank": 3,
                "title": f"{board}{dimension}标题",
                "author_name": f"{board}账号",
                "publish_date": "2026-08-18 09:30:00",
                "read_range": "10w+",
                "click_rate_range": "5%-10%",
                "pay_rate_range": "1%-2%",
                "gmv_range": "1w-5w",
                "note_id": f"{len(self.calls):024d}",
                "user_id": f"user-{len(self.calls)}",
            },
        )
        return CollectionResult(
            status="succeeded",
            evidence_artifacts=[relative.as_posix()],
            items=[item],
            expected_count_known=True,
            expected_count=1,
            succeeded_count=1,
            overflow_count=0,
            complete=True,
        )


def _capturing_submitter(scheduled: list[tuple[Any, tuple[Any, ...]]]):
    def submit(action: Any, *args: Any) -> None:
        scheduled.append((action, args))

    return submit


def _service(
    tmp_path: Path,
    *,
    partial_scope: tuple[str, str] | None = None,
) -> tuple[QianfanCollectionService, JobService, RadarService, _ExactScopeAdapter, list[tuple[Any, tuple[Any, ...]]]]:
    runtime = tmp_path / "runtime"
    database = Database(runtime / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime)
    radar = RadarService(database)
    adapter = _ExactScopeAdapter(jobs, runtime, partial_scope=partial_scope)
    scheduled: list[tuple[Any, tuple[Any, ...]]] = []
    service = QianfanCollectionService(
        job_service=jobs,
        radar_service=radar,
        runtime_dir=runtime,
        adapter_factory=lambda: adapter,
        selector_profile=_controlled_profile(),
        submitter=_capturing_submitter(scheduled),
        clock=lambda: datetime(2026, 8, 18, 2, 30, tzinfo=UTC),
    )
    return service, jobs, radar, adapter, scheduled


def test_server_scope_matrix_and_default_profile_are_fixed_and_truthful() -> None:
    assert QIANFAN_SCOPES == (
        ("阅读榜", "优秀内容"),
        ("阅读榜", "优秀账号"),
        ("引流榜", "优秀内容"),
        ("引流榜", "优秀账号"),
        ("热卖榜", "优秀内容"),
        ("热卖榜", "优秀账号"),
        ("成交榜", "优秀内容"),
        ("成交榜", "优秀账号"),
    )
    assert DEFAULT_QIANFAN_SELECTOR_PROFILE.supported is True
    assert DEFAULT_QIANFAN_SELECTOR_PROFILE.version == "qianfan-note-rank-live-v1"
    assert dict(DEFAULT_QIANFAN_SELECTOR_PROFILE.board_request_mapping) == {
        "阅读榜": 1,
        "引流榜": 2,
        "热卖榜": 3,
        "成交榜": 4,
    }
    controlled = _controlled_profile()
    with pytest.raises(TypeError):
        controlled.board_selectors["阅读榜"] = "mutated"  # type: ignore[index]

    with pytest.raises(ValueError):
        QianfanCollectionCreate.model_validate(
            {
                "expected_count_per_scope": 20,
                "url": "https://evil.example/",
                "selector": "*",
                "javascript": "alert(1)",
                "profile_path": "D:/stolen",
                "selector_profile_version": "attacker-v1",
            }
        )


def test_enqueue_reserves_exactly_eight_scope_jobs_before_one_serial_submission(tmp_path: Path) -> None:
    service, jobs, _radar, _adapter, scheduled = _service(tmp_path)

    queued = service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))

    assert len(queued.scopes) == 8
    assert [(scope.board, scope.dimension) for scope in queued.scopes] == list(QIANFAN_SCOPES)
    assert len({scope.job_id for scope in queued.scopes}) == 8
    assert len(scheduled) == 1
    for scope in queued.scopes:
        durable = jobs.get(scope.job_id)
        assert durable.state is JobState.queued
        assert durable.progress_current == 0
        assert durable.progress_total == 1
        assert durable.input == {
            "collection_id": queued.collection_id,
            "board": scope.board,
            "dimension": scope.dimension,
            "expected_count": 1,
            "selector_profile_version": "qianfan-controlled-test-v1",
        }


def test_controlled_empty_database_run_maps_all_fields_and_creates_eight_bound_snapshots(tmp_path: Path) -> None:
    service, jobs, radar, adapter, scheduled = _service(tmp_path)
    queued = service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
    action, args = scheduled.pop()

    action(*args)

    snapshots = radar.list_snapshots(source_date="2026-08-18")
    assert len(snapshots) == 8
    assert {(row.board, row.dimension) for row in snapshots} == set(QIANFAN_SCOPES)
    assert len(set(adapter.thread_ids)) == 1
    assert adapter.calls == [(board, dimension, 1) for board, dimension in QIANFAN_SCOPES]
    for scope in queued.scopes:
        job = jobs.get(scope.job_id)
        assert job.state is JobState.succeeded
        assert job.progress_current == 1
        assert job.progress_total == 1
        assert len(job.artifacts) == 1
        snapshot = next(
            row for row in snapshots if (row.board, row.dimension) == (scope.board, scope.dimension)
        )
        binding = snapshot.raw_evidence["collection_binding"]
        assert binding == {
            "job_id": scope.job_id,
            "collection_id": queued.collection_id,
            "artifact_path": job.artifacts[0].path,
            "artifact_sha256": job.artifacts[0].metadata["sha256"],
            "source_url": QIANFAN_RANK_URL,
            "selector_profile_version": "qianfan-controlled-test-v1",
            "board": scope.board,
            "dimension": scope.dimension,
            "expected_count": 1,
            "succeeded_count": 1,
            "observed_count": 1,
            "raw_observation_count": 1,
            "duplicate_observation_count": 0,
            "rejected_count": 0,
            "missing_count": 0,
            "overflow_count": 0,
            "complete": True,
        }
        item = snapshot.items[0]
        assert item.rank_no == 3
        assert item.title == f"{scope.board}{scope.dimension}标题"
        assert item.author_name == f"{scope.board}账号"
        assert item.publish_date == "2026-08-18 09:30:00"
        assert item.read_range == "10w+"
        assert item.click_rate_range == "5%-10%"
        assert item.pay_rate_range == "1%-2%"
        assert item.gmv_range == "1w-5w"
        assert item.note_id
        assert item.user_id
        assert item.raw_evidence["item"]["userFansNum"] == 321


def test_partial_scope_is_needs_human_and_seven_snapshots_never_become_eight(tmp_path: Path) -> None:
    failed_scope = ("热卖榜", "优秀账号")
    service, jobs, radar, _adapter, scheduled = _service(tmp_path, partial_scope=failed_scope)
    queued = service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
    action, args = scheduled.pop()

    action(*args)

    assert len(radar.list_snapshots(source_date="2026-08-18")) == 7
    scope = next(row for row in queued.scopes if (row.board, row.dimension) == failed_scope)
    job = jobs.get(scope.job_id)
    assert job.state is JobState.needs_human
    assert job.progress_current == 0
    assert job.progress_total == 1
    assert job.error_category == "expected_items_missing"

    service.close()
    assert jobs.get(scope.job_id).state is JobState.needs_human


def test_same_day_partial_rerun_hides_prior_batch_scope_instead_of_mixing_eight(tmp_path: Path) -> None:
    service, _jobs, radar, adapter, scheduled = _service(tmp_path)
    first = service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
    action, args = scheduled.pop()
    action(*args)
    assert len(radar.list_snapshots(source_date="2026-08-18")) == 8

    adapter.partial_scope = ("热卖榜", "优秀账号")
    second = service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
    action, args = scheduled.pop()
    action(*args)

    snapshots = radar.list_snapshots(source_date="2026-08-18")
    assert len(snapshots) == 7
    assert first.collection_id != second.collection_id
    assert {
        row.raw_evidence["collection_binding"]["collection_id"] for row in snapshots
    } == {second.collection_id}


def test_same_timestamp_reverse_uuid_still_uses_later_collection_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.radar.qianfan_service as qianfan_module

    identifiers = iter(
        [
            "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "00000000-0000-4000-8000-000000000000",
        ]
    )
    monkeypatch.setattr(qianfan_module, "uuid4", lambda: next(identifiers))
    service, jobs, radar, adapter, scheduled = _service(tmp_path)
    first = service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
    action, args = scheduled.pop()
    action(*args)

    second = service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
    with jobs.database.session() as session:
        rows = session.scalars(
            update(JobRecord)
            .where(JobRecord.type == "qianfan_ranking_scope")
            .values(created_at=datetime(2026, 8, 18, 12, 0))
            .returning(JobRecord)
        ).all()
        assert len(rows) == 16
        session.commit()
    adapter.partial_scope = ("热卖榜", "优秀账号")
    action, args = scheduled.pop()
    action(*args)

    snapshots = radar.list_snapshots(source_date="2026-08-18")
    assert first.collection_id.startswith("ffffffff")
    assert second.collection_id.startswith("00000000")
    assert len(snapshots) == 7
    assert {
        row.raw_evidence["collection_binding"]["collection_id"] for row in snapshots
    } == {second.collection_id}


def test_source_date_is_generated_when_worker_executes_not_when_http_enqueues(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    database = Database(runtime / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime)
    radar = RadarService(database)
    adapter = _ExactScopeAdapter(jobs, runtime)
    scheduled: list[tuple[Any, tuple[Any, ...]]] = []
    current = [datetime(2026, 8, 18, 15, 59, tzinfo=UTC)]
    service = QianfanCollectionService(
        job_service=jobs,
        radar_service=radar,
        runtime_dir=runtime,
        adapter_factory=lambda: adapter,
        selector_profile=_controlled_profile(),
        submitter=_capturing_submitter(scheduled),
        clock=lambda: current[0],
    )

    service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
    current[0] = datetime(2026, 8, 19, 0, 1, tzinfo=UTC)
    action, args = scheduled.pop()
    action(*args)

    assert radar.list_snapshots(source_date="2026-08-18") == []
    assert len(radar.list_snapshots(source_date="2026-08-19")) == 8


def test_cancellation_wins_atomically_before_snapshot_and_job_success(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    database = Database(runtime / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime)

    class CancelBeforeCommitRadar(RadarService):
        target_job_id: str | None = None

        def ingest_snapshot(self, snapshot: Any) -> Any:
            assert self.target_job_id is not None
            jobs.transition(self.target_job_id, JobState.cancelled)
            return super().ingest_snapshot(snapshot)

        def ingest_snapshot_and_finalize_job(self, snapshot: Any, **kwargs: Any) -> Any:
            assert self.target_job_id is not None
            jobs.transition(self.target_job_id, JobState.cancelled)
            return super().ingest_snapshot_and_finalize_job(snapshot, **kwargs)

    radar = CancelBeforeCommitRadar(database)
    adapter = _ExactScopeAdapter(jobs, runtime)
    scheduled: list[tuple[Any, tuple[Any, ...]]] = []
    service = QianfanCollectionService(
        job_service=jobs,
        radar_service=radar,
        runtime_dir=runtime,
        adapter_factory=lambda: adapter,
        selector_profile=_controlled_profile(),
        submitter=_capturing_submitter(scheduled),
        clock=lambda: datetime(2026, 8, 18, 2, 30, tzinfo=UTC),
    )
    queued = service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
    radar.target_job_id = queued.scopes[0].job_id
    action, args = scheduled.pop()

    action(*args)

    assert jobs.get(queued.scopes[0].job_id).state is JobState.cancelled
    assert not any(
        (row.board, row.dimension)
        == (queued.scopes[0].board, queued.scopes[0].dimension)
        for row in radar.list_snapshots(source_date="2026-08-18")
    )


def test_wrong_source_artifact_cannot_be_relabelled_as_qianfan_snapshot(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    database = Database(runtime / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime)
    radar = RadarService(database)
    adapter = _ExactScopeAdapter(
        jobs,
        runtime,
        artifact_source_url="https://wrong.example/rank",
    )
    scheduled: list[tuple[Any, tuple[Any, ...]]] = []
    service = QianfanCollectionService(
        job_service=jobs,
        radar_service=radar,
        runtime_dir=runtime,
        adapter_factory=lambda: adapter,
        selector_profile=_controlled_profile(),
        submitter=_capturing_submitter(scheduled),
        clock=lambda: datetime(2026, 8, 18, 2, 30, tzinfo=UTC),
    )
    queued = service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
    action, args = scheduled.pop()

    action(*args)

    assert radar.list_snapshots(source_date="2026-08-18") == []
    for scope in queued.scopes:
        job = jobs.get(scope.job_id)
        assert job.state is JobState.needs_human
        assert job.error_category == "artifact_binding_unverified"


@pytest.mark.anyio
async def test_http_202_starts_only_server_owned_eight_scope_collection(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            runtime_dir=tmp_path / "runtime",
            database_path=tmp_path / "runtime" / "db.sqlite3",
            xhs_cli_state_dir=tmp_path / "runtime" / "xhs-cli-state",
        )
    )
    original = app.state.qianfan_collection_service
    original.close()
    scheduled: list[tuple[Any, tuple[Any, ...]]] = []
    adapter = _ExactScopeAdapter(app.state.job_service, tmp_path / "runtime")
    app.state.qianfan_collection_service = QianfanCollectionService(
        job_service=app.state.job_service,
        radar_service=app.state.radar_service,
        runtime_dir=tmp_path / "runtime",
        adapter_factory=lambda: adapter,
        selector_profile=_controlled_profile(),
        submitter=_capturing_submitter(scheduled),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/radar/qianfan-collections",
            json={"expected_count_per_scope": 1},
        )

    assert response.status_code == 202
    body = response.json()
    assert len(body["scopes"]) == 8
    assert len(scheduled) == 1
    app.state.qianfan_collection_service.close()
    app.state.database.close()


@pytest.mark.anyio
async def test_public_jobs_api_cannot_forge_qianfan_job_or_raw_artifact(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    app = create_app(
        Settings(
            runtime_dir=runtime,
            database_path=runtime / "db.sqlite3",
            xhs_cli_state_dir=runtime / "xhs-cli-state",
        )
    )
    generic = app.state.job_service.create(job_type="generic", input_data={})
    evidence = runtime / "evidence" / "forged.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("{}", encoding="utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        created = await client.post(
            "/api/v1/jobs",
            json={"type": "qianfan_ranking_scope", "input": {}},
        )
        attached = await client.post(
            f"/api/v1/jobs/{generic.id}/artifacts",
            json={"kind": "qianfan_raw_capture", "path": "evidence/forged.json"},
        )

    assert created.status_code == 422
    assert attached.status_code == 422


def test_startup_recovers_reserved_or_running_scope_jobs_as_needs_human(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    database = Database(runtime / "db.sqlite3")
    jobs = JobService(database, runtime_dir=runtime)
    queued = jobs.create(job_type="qianfan_ranking_scope", input_data={})
    running = jobs.create(job_type="qianfan_ranking_scope", input_data={})
    jobs.claim(running.id)

    service = QianfanCollectionService(
        job_service=jobs,
        radar_service=RadarService(database),
        runtime_dir=runtime,
        adapter_factory=None,
        submitter=_capturing_submitter([]),
    )

    for job_id in (queued.id, running.id):
        recovered = jobs.get(job_id)
        assert recovered.state is JobState.needs_human
        assert recovered.current_stage == "worker_restart_required"
        assert recovered.error_category == "worker_restart_required"
    service.close()


def test_blocked_browser_worker_cannot_keep_python_process_alive(tmp_path: Path) -> None:
    runtime = tmp_path / "child-runtime"
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from threading import Event
        from backend.app.adapters.contracts import CollectionRequest
        from backend.app.db import Database
        from backend.app.features.radar.qianfan_service import QianfanCollectionCreate, QianfanCollectionService
        from backend.app.features.radar.service import RadarService
        from backend.app.services.jobs import JobService
        from backend.tests.radar.test_qianfan_orchestration import _controlled_profile

        runtime = Path({str(runtime)!r})
        database = Database(runtime / 'db.sqlite3')
        jobs = JobService(database, runtime_dir=runtime)
        started = Event()

        class Blocked:
            def collect_scope(self, request: CollectionRequest, **kwargs):
                started.set()
                Event().wait()

        service = QianfanCollectionService(
            job_service=jobs,
            radar_service=RadarService(database),
            runtime_dir=runtime,
            adapter_factory=lambda: Blocked(),
            selector_profile=_controlled_profile(),
        )
        service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
        assert started.wait(1)
        service.close()
        print('lifespan-returned', flush=True)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=2)
        pytest.fail(
            f"blocked Qianfan worker kept Python alive; stdout={stdout!r}, stderr={stderr!r}"
        )
    assert process.returncode == 0, stderr
    assert "lifespan-returned" in stdout


class _ScopeLocator:
    def __init__(self, page: "_ScopePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    def count(self) -> int:
        return self.page.counts.get(self.selector, 0)

    def click(self) -> None:
        self.page.clicks.append(self.selector)
        self.page.emit()


class _ScopeRequest:
    method = "POST"

    def __init__(self, *, url: str, board: str) -> None:
        self.url = url
        self._data = {
            "sortBy": {"阅读榜": 1, "引流榜": 2, "热卖榜": 3, "成交榜": 4}[board],
            "noteType": 0,
            "pageNo": 1,
            "pageSize": 10,
        }

    def post_data_json(self) -> dict[str, Any]:
        return self._data


class _ScopeResponse:
    status = 200

    def __init__(
        self, board: str, dimension: str, items: list[dict[str, Any]] | None = None
    ) -> None:
        self.board = board
        self.dimension = dimension
        self.items = items or []
        path = (
            "/api/edith/business/data/note/user/rank/v2/list"
            if dimension == "优秀账号"
            else "/api/edith/business/data/note/rank/v2/list"
        )
        self.url = f"https://ark.xiaohongshu.com{path}"
        self.request = _ScopeRequest(url=self.url, board=board)

    def json(self) -> dict[str, Any]:
        return {"data": {"dataList": self.items}}

    def text(self) -> str:
        return json.dumps(self.json(), ensure_ascii=False)


class _ScopePage:
    url = QIANFAN_RANK_URL

    def __init__(self, response: _ScopeResponse) -> None:
        self.response = response
        self.handler: Any = None
        self.clicks: list[str] = []
        self.counts = {"login": 0, "captcha": 0, "ready": 1, "active-board": 1, "active-dimension": 1}

    def on(self, _event: str, handler: Any) -> None:
        self.handler = handler

    def remove_listener(self, _event: str, _handler: Any) -> None:
        self.handler = None

    def goto(self, _url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"

    def locator(self, selector: str) -> _ScopeLocator:
        return _ScopeLocator(self, selector)

    def content(self) -> str:
        return "<main>controlled scope</main>"

    def emit(self) -> None:
        if callable(self.handler) and len(self.clicks) == 2:
            self.handler(self.response)


def _controlled_profile() -> QianfanSelectorProfile:
    return QianfanSelectorProfile(
        version="qianfan-controlled-test-v1",
        supported=True,
        ready_selector="ready",
        login_selector="login",
        captcha_selector="captcha",
        board_selectors={board: f"board:{board}" for board, _ in QIANFAN_SCOPES},
        dimension_selectors={dimension: f"dimension:{dimension}" for _, dimension in QIANFAN_SCOPES},
        active_board_selectors={board: "active-board" for board, _ in QIANFAN_SCOPES},
        active_dimension_selectors={dimension: "active-dimension" for _, dimension in QIANFAN_SCOPES},
        response_board_path=("data", "boardName"),
        response_dimension_path=("data", "dimensionName"),
    )


def test_adapter_requires_exact_ui_and_response_scope_before_completion() -> None:
    profile = _controlled_profile()
    page = _ScopePage(_ScopeResponse("阅读榜", "优秀账号"))
    adapter = QianfanPlaywrightAdapter(page_factory=lambda: page, timeout_seconds=0)

    mismatch = adapter.collect_scope(
        CollectionRequest(capability="rankings", expected_count=0),
        board="阅读榜",
        dimension="优秀内容",
        selector_profile=profile,
    )

    assert mismatch.status == "needs_human"
    assert mismatch.detail == "scope_unverified"
    assert mismatch.complete is False
    assert page.clicks == ["board:阅读榜", "dimension:优秀内容"]


def test_adapter_maps_complete_qianfan_row_only_after_exact_scope_verification() -> None:
    profile = _controlled_profile()
    page = _ScopePage(
        _ScopeResponse(
            "成交榜",
            "优秀账号",
            [
                {
                    "rank": 2,
                    "noteId": "abc_123",
                    "noteTitle": "完整标题",
                    "userId": "user-7",
                    "userNickname": "真实账号",
                    "publishTime": "2026-08-18 10:00:00",
                    "noteReadNumRange": "10w+",
                    "noteClickRateRange": "5%-10%",
                    "notePayRateRange": "1%-2%",
                    "noteGmvRange": "1w-5w",
                }
            ],
        )
    )
    adapter = QianfanPlaywrightAdapter(page_factory=lambda: page, timeout_seconds=0)

    result = adapter.collect_scope(
        CollectionRequest(capability="rankings", expected_count=1),
        board="成交榜",
        dimension="优秀账号",
        selector_profile=profile,
    )

    assert result.status == "succeeded"
    assert result.complete is True
    assert result.items[0].data == {
        "rank": 2,
        "author_name": "真实账号",
        "user_id": "user-7",
    }


def test_unsupported_selector_profile_never_opens_a_browser() -> None:
    opened = Event()
    adapter = QianfanPlaywrightAdapter(page_factory=lambda: opened.set())

    result = adapter.collect_scope(
        CollectionRequest(capability="rankings", expected_count=20),
        board="成交榜",
        dimension="优秀账号",
        selector_profile=QianfanSelectorProfile(
            version="unsupported-controlled-profile",
            supported=False,
        ),
    )

    assert result.status == "needs_human"
    assert result.detail == "selector_profile_unverified"
    assert result.complete is False
    assert opened.is_set() is False
