"""Durable serial orchestration for the fixed Qianfan eight-scope ranking pass."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import Future
from datetime import date, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import RLock, Thread
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from backend.app.adapters.contracts import CollectionRequest, CollectionResult
from backend.app.adapters.qianfan_playwright import (
    DEFAULT_QIANFAN_SELECTOR_PROFILE,
    QIANFAN_RANK_URL,
    QianfanSelectorProfile,
)
from backend.app.features.radar.models import (
    BoardName,
    DimensionName,
    RankItemInput,
    RankSnapshotInput,
)
from backend.app.features.radar.service import RadarService
from backend.app.models.jobs import JobState
from backend.app.services.jobs import (
    InvalidJobTransition,
    Job,
    JobCreateSpec,
    JobService,
)


QIANFAN_SCOPE_JOB_TYPE = "qianfan_ranking_scope"
QIANFAN_SCOPES: tuple[tuple[BoardName, DimensionName], ...] = (
    ("阅读榜", "优秀内容"),
    ("阅读榜", "优秀账号"),
    ("引流榜", "优秀内容"),
    ("引流榜", "优秀账号"),
    ("热卖榜", "优秀内容"),
    ("热卖榜", "优秀账号"),
    ("成交榜", "优秀内容"),
    ("成交榜", "优秀账号"),
)


class QianfanCollectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_count_per_scope: StrictInt = Field(ge=1, le=1000)


class QianfanScopeQueued(BaseModel):
    job_id: str
    board: BoardName
    dimension: DimensionName
    status: Literal["queued"] = "queued"


class QianfanCollectionQueued(BaseModel):
    collection_id: str
    scopes: list[QianfanScopeQueued] = Field(min_length=8, max_length=8)


class QianfanCollectionServiceClosed(RuntimeError):
    """Raised when shutdown has closed admission before any job reservation."""


class QianfanScopeValidationError(RuntimeError):
    """Raised when exact scope, evidence, mapping, or N/N trust cannot be proven."""


class _DaemonSerialWorker:
    """One lazy daemon worker so a blocked provider cannot hold process exit."""

    def __init__(self) -> None:
        self._queue: Queue[tuple[Future[Any], Callable[..., Any], tuple[Any, ...]] | None] = Queue()
        self._lock = RLock()
        self._closed = False
        self._thread: Thread | None = None

    def submit(self, action: Callable[..., Any], *args: Any) -> Future[Any]:
        future: Future[Any] = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("Qianfan serial worker is closed.")
            if self._thread is None:
                self._thread = Thread(
                    target=self._run,
                    name="qianfan-ranking",
                    daemon=True,
                )
                self._thread.start()
            self._queue.put((future, action, args))
        return future

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    item = self._queue.get_nowait()
                except Empty:
                    break
                if item is not None:
                    item[0].cancel()
                self._queue.task_done()
            self._queue.put(None)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                future, action, args = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(action(*args))
                except BaseException as error:
                    future.set_exception(error)
            finally:
                self._queue.task_done()


class QianfanCollectionService:
    """Reserve eight durable jobs and execute every scope on one serial worker."""

    def __init__(
        self,
        *,
        job_service: JobService,
        radar_service: RadarService,
        runtime_dir: Path,
        adapter_factory: Callable[[], Any] | None,
        selector_profile: QianfanSelectorProfile = DEFAULT_QIANFAN_SELECTOR_PROFILE,
        submitter: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.job_service = job_service
        self.radar_service = radar_service
        self.runtime_dir = runtime_dir.resolve()
        self.adapter_factory = adapter_factory
        self.selector_profile = selector_profile
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.job_service.recover_interrupted_workers(job_type=QIANFAN_SCOPE_JOB_TYPE)
        self._worker = _DaemonSerialWorker() if submitter is None else None
        self._submitter = submitter or self._worker.submit
        self._lock = RLock()
        self._accepting = True
        self._futures: set[Future[Any]] = set()

    def enqueue(self, payload: QianfanCollectionCreate) -> QianfanCollectionQueued:
        with self._lock:
            if not self._accepting:
                raise QianfanCollectionServiceClosed(
                    "Qianfan collection service is closed."
                )
            collection_id = str(uuid4())
            jobs = self.job_service.create_batch(
                [
                    JobCreateSpec(
                        job_type=QIANFAN_SCOPE_JOB_TYPE,
                        input_data={
                            "collection_id": collection_id,
                            "board": board,
                            "dimension": dimension,
                            "expected_count": payload.expected_count_per_scope,
                            "selector_profile_version": self.selector_profile.version,
                        },
                        progress_total=payload.expected_count_per_scope,
                        current_stage="qianfan_scope_reserved",
                    )
                    for board, dimension in QIANFAN_SCOPES
                ]
            )
            scopes = [
                QianfanScopeQueued(job_id=job.id, board=board, dimension=dimension)
                for job, (board, dimension) in zip(jobs, QIANFAN_SCOPES, strict=True)
            ]
            try:
                submitted = self._submitter(
                    self.execute_batch,
                    collection_id,
                    tuple(scopes),
                    payload.expected_count_per_scope,
                )
            except Exception:
                self._fail_reserved(scopes, "qianfan_collection_schedule_failed")
                raise
            if isinstance(submitted, Future):
                self._futures.add(submitted)
                submitted.add_done_callback(self._future_finished)
            return QianfanCollectionQueued(
                collection_id=collection_id,
                scopes=scopes,
            )

    def execute_batch(
        self,
        collection_id: str,
        scopes: tuple[QianfanScopeQueued, ...],
        expected_count: int,
    ) -> None:
        collection_started_at = self.clock()
        source_date = collection_started_at.date()
        try:
            self.radar_service.start_qianfan_collection(
                collection_id=collection_id,
                source_date=source_date,
                started_at=collection_started_at,
                expected_count_per_scope=expected_count,
                selector_profile_version=self.selector_profile.version,
            )
        except Exception:
            self._fail_reserved(list(scopes), "qianfan_collection_start_failed")
            return
        for scope in scopes:
            with self._lock:
                accepting = self._accepting
            if not accepting:
                self._cancel_reserved(scope.job_id)
                continue
            self._execute_scope(
                scope,
                expected_count,
                collection_id=collection_id,
                source_date=source_date,
            )

    def _execute_scope(
        self,
        scope: QianfanScopeQueued,
        expected_count: int,
        *,
        collection_id: str,
        source_date: date,
    ) -> None:
        try:
            self.job_service.claim(scope.job_id)
        except InvalidJobTransition:
            return
        try:
            if self.adapter_factory is None:
                self._persist_service_evidence(
                    scope,
                    reason="browser_not_configured",
                )
                self.job_service.transition(
                    scope.job_id,
                    JobState.needs_human,
                    progress_current=0,
                    progress_total=expected_count,
                    current_stage="qianfan_needs_human",
                    error_category="browser_not_configured",
                )
                return
            adapter = self.adapter_factory()
            result = adapter.collect_scope(
                CollectionRequest(
                    capability="rankings",
                    parameters={
                        "job_id": scope.job_id,
                        "is_cancelled": self._shutdown_requested,
                    },
                    expected_count=expected_count,
                ),
                board=scope.board,
                dimension=scope.dimension,
                selector_profile=self.selector_profile,
            )
            with self._lock:
                accepting = self._accepting
            if not accepting:
                self._cancel_running(scope.job_id)
                return
            if not _is_exact_complete(result, expected_count=expected_count):
                self._finalize_incomplete(scope.job_id, result, expected_count)
                return
            now = self.clock()
            snapshot = self._build_snapshot(
                scope,
                result,
                collection_id=collection_id,
                source_date=source_date,
                now=now,
            )
            self.radar_service.ingest_snapshot_and_finalize_job(
                snapshot,
                job_id=scope.job_id,
                progress_current=result.succeeded_count,
                progress_total=expected_count,
            )
        except QianfanScopeValidationError as error:
            self._needs_human_after_error(
                scope.job_id,
                category=str(error),
                expected_count=expected_count,
            )
        except Exception as error:
            if self._shutdown_requested():
                self._cancel_running(scope.job_id)
                return
            self._failed_after_error(
                scope,
                category="qianfan_scope_execution_failed",
                error=error,
            )

    def _build_snapshot(
        self,
        scope: QianfanScopeQueued,
        result: CollectionResult,
        *,
        collection_id: str,
        source_date: date,
        now: datetime,
    ) -> RankSnapshotInput:
        job = self.job_service.get(scope.job_id)
        artifact = _exact_capture_artifact(job, result)
        absolute = (self.runtime_dir / artifact.path).resolve()
        try:
            absolute.relative_to(self.runtime_dir)
        except ValueError as error:
            raise QianfanScopeValidationError("artifact_path_untrusted") from error
        if not absolute.is_file():
            raise QianfanScopeValidationError("artifact_missing")
        digest = hashlib.sha256(absolute.read_bytes()).hexdigest()
        metadata = artifact.metadata
        if (
            metadata.get("sha256") != digest
            or metadata.get("source_url") != QIANFAN_RANK_URL
            or metadata.get("selector_profile_version") != self.selector_profile.version
            or metadata.get("board") != scope.board
            or metadata.get("dimension") != scope.dimension
        ):
            raise QianfanScopeValidationError("artifact_binding_unverified")
        items: list[RankItemInput] = []
        try:
            for item in result.items:
                data = item.data
                items.append(
                    RankItemInput(
                        rank_no=data.get("rank"),
                        title=data.get("title"),
                        author_name=data.get("author_name"),
                        publish_date=data.get("publish_date"),
                        read_range=data.get("read_range"),
                        click_rate_range=data.get("click_rate_range"),
                        pay_rate_range=data.get("pay_rate_range"),
                        gmv_range=data.get("gmv_range"),
                        note_id=data.get("note_id"),
                        user_id=data.get("user_id"),
                        source_url=item.source_url,
                        raw_evidence=item.raw_evidence,
                    )
                )
        except Exception as error:
            raise QianfanScopeValidationError("ranking_mapping_unverified") from error
        if len(items) != result.succeeded_count:
            raise QianfanScopeValidationError("ranking_mapping_unverified")
        binding = {
            "job_id": scope.job_id,
            "collection_id": collection_id,
            "artifact_path": artifact.path,
            "artifact_sha256": digest,
            "source_url": QIANFAN_RANK_URL,
            "selector_profile_version": self.selector_profile.version,
            "board": scope.board,
            "dimension": scope.dimension,
            "expected_count": result.expected_count,
            "succeeded_count": result.succeeded_count,
            "observed_count": result.observed_count,
            "raw_observation_count": result.raw_observation_count,
            "duplicate_observation_count": result.duplicate_observation_count,
            "rejected_count": len(result.rejected_items),
            "missing_count": len(result.missing_items),
            "overflow_count": result.overflow_count,
            "complete": result.complete,
        }
        return RankSnapshotInput(
            source_date=source_date,
            collected_at=now,
            board=scope.board,
            dimension=scope.dimension,
            source_url=QIANFAN_RANK_URL,
            raw_evidence={"collection_binding": binding},
            items=items,
        )

    def _finalize_incomplete(
        self,
        job_id: str,
        result: CollectionResult,
        expected_count: int,
    ) -> None:
        state = JobState.failed if result.status == "failed" else JobState.needs_human
        self.job_service.transition(
            job_id,
            state,
            progress_current=result.succeeded_count,
            progress_total=expected_count,
            current_stage=(
                "qianfan_scope_failed"
                if state is JobState.failed
                else "qianfan_needs_human"
            ),
            error_category=result.detail or result.status,
        )

    def _persist_service_evidence(
        self, scope: QianfanScopeQueued, *, reason: str
    ) -> None:
        payload = {
            "status": "needs_human",
            "reason": reason,
            "requested_scope": {"board": scope.board, "dimension": scope.dimension},
            "selector_profile_version": self.selector_profile.version,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        relative = Path("evidence") / "qianfan" / f"{scope.job_id}-service.json"
        absolute = self.runtime_dir / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(encoded)
        self.job_service.attach_artifact(
            scope.job_id,
            kind="qianfan_raw_capture",
            path=relative.as_posix(),
            metadata={
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "source_url": QIANFAN_RANK_URL,
                "selector_profile_version": self.selector_profile.version,
                "board": scope.board,
                "dimension": scope.dimension,
                "reason": reason,
            },
        )

    def _needs_human_after_error(
        self, job_id: str, *, category: str, expected_count: int
    ) -> None:
        job = self.job_service.get(job_id)
        if job.state is JobState.running:
            self.job_service.transition(
                job_id,
                JobState.needs_human,
                progress_current=0,
                progress_total=expected_count,
                current_stage="qianfan_needs_human",
                error_category=category,
            )

    def _failed_after_error(
        self, scope: QianfanScopeQueued, *, category: str, error: Exception
    ) -> None:
        try:
            job_id = scope.job_id
            if not any(
                artifact.kind == "qianfan_raw_capture"
                for artifact in self.job_service.get(job_id).artifacts
            ):
                self._persist_service_evidence(scope, reason=category)
            self.job_service.append_log(
                job_id,
                level="error",
                message=f"Qianfan scope execution raised: {type(error).__name__}.",
            )
            if self.job_service.get(job_id).state is JobState.running:
                self.job_service.transition(
                    job_id,
                    JobState.failed,
                    current_stage="qianfan_scope_failed",
                    error_category=category,
                )
        except Exception:
            return

    def _fail_reserved(
        self, scopes: list[QianfanScopeQueued], category: str
    ) -> None:
        for scope in scopes:
            try:
                self.job_service.claim(scope.job_id)
                self.job_service.transition(
                    scope.job_id,
                    JobState.failed,
                    current_stage="qianfan_scope_failed",
                    error_category=category,
                )
            except Exception:
                continue

    def _cancel_reserved(self, job_id: str) -> None:
        try:
            job = self.job_service.get(job_id)
            if job.state is JobState.queued:
                self.job_service.transition(
                    job_id,
                    JobState.cancelled,
                    current_stage="worker_shutdown_cancelled",
                    error_category="worker_shutdown_cancelled",
                )
        except Exception:
            return

    def _cancel_running(self, job_id: str) -> None:
        try:
            if self.job_service.get(job_id).state is JobState.running:
                self.job_service.transition(
                    job_id,
                    JobState.cancelled,
                    current_stage="worker_shutdown_cancelled",
                    error_category="worker_shutdown_cancelled",
                )
        except Exception:
            return

    def _future_finished(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)

    def _shutdown_requested(self) -> bool:
        with self._lock:
            return not self._accepting

    def close(self) -> None:
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
            futures = tuple(self._futures)
        for future in futures:
            future.cancel()
        for job in self.job_service.list():
            if job.type != QIANFAN_SCOPE_JOB_TYPE:
                continue
            if job.state is JobState.queued:
                self._cancel_reserved(job.id)
            elif job.state is JobState.running:
                self._cancel_running(job.id)
        if self._worker is not None:
            self._worker.close()


def _is_exact_complete(result: CollectionResult, *, expected_count: int) -> bool:
    return (
        result.status == "succeeded"
        and result.complete
        and result.expected_count_known
        and result.expected_count == expected_count
        and result.succeeded_count == expected_count
        and result.observed_count == expected_count
        and result.raw_observation_count == expected_count
        and result.duplicate_observation_count == 0
        and not result.rejected_items
        and not result.missing_items
        and result.overflow_count == 0
        and len(result.evidence_artifacts) == 1
    )


def _exact_capture_artifact(job: Job, result: CollectionResult) -> Any:
    if len(result.evidence_artifacts) != 1:
        raise QianfanScopeValidationError("artifact_binding_unverified")
    path = result.evidence_artifacts[0]
    matches = [
        artifact
        for artifact in job.artifacts
        if artifact.kind == "qianfan_raw_capture" and artifact.path == path
    ]
    if len(matches) != 1:
        raise QianfanScopeValidationError("artifact_binding_unverified")
    return matches[0]
