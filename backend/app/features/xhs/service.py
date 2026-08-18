"""Durable orchestration for read-only XHS account and note collection."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import RLock, Thread
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from backend.app.adapters.contracts import CollectionItem, CollectionRequest, CollectionResult
from backend.app.db import Database
from backend.app.features.xhs.constants import (
    ACCOUNT_COLLECTION_ARTIFACT_KIND,
    ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
    ACCOUNT_COLLECTION_JOB_TYPE,
)
from backend.app.features.xhs.models import XhsAccountNoteRecord, XhsAccountProfileRecord
from backend.app.features.xhs.schemas import AccountEvidenceBinding, persist_exact_account_result
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState
from backend.app.services.jobs import InvalidJobTransition, Job, JobService


SEARCH_COLLECTION_JOB_TYPE = "xhs_note_search"
SEARCH_COLLECTION_ARTIFACT_KIND = "xhs_note_search_raw"
XHS_RESERVED_JOB_TYPES = (ACCOUNT_COLLECTION_JOB_TYPE, SEARCH_COLLECTION_JOB_TYPE)
XHS_RESERVED_ARTIFACT_KINDS = (
    ACCOUNT_COLLECTION_ARTIFACT_KIND,
    SEARCH_COLLECTION_ARTIFACT_KIND,
)
_SAFE_SUBJECT = re.compile(r"^[A-Za-z0-9_-]{1,500}$")
_SENSITIVE_KEY = re.compile(
    r"(?:cookie|token|credential|authorization|password|secret|session|api[_-]?key)",
    re.IGNORECASE,
)


class CollectionServiceClosed(RuntimeError):
    """Admission has closed and no new durable reservation was created."""


class CollectionFactNotFound(LookupError):
    """A requested normalized collection fact does not exist."""


class _ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class AccountProfileRead(_ReadModel):
    user_id: str
    source_url: str
    nickname: str | None
    bio: str | None
    public_stats: dict[str, Any]
    collection_job_id: str
    collection_artifact_id: int
    collected_at: datetime


class AccountNoteRead(_ReadModel):
    note_id: str
    user_id: str
    source_url: str
    title: str | None
    summary: str | None
    published_at: str | None
    public_interactions: dict[str, Any]
    collection_job_id: str
    collection_artifact_id: int
    collected_at: datetime


class SearchNoteRead(_ReadModel):
    note_id: str
    source_url: str
    title: str | None = None
    summary: str | None = None
    user_id: str | None = None


class SearchResultsRead(_ReadModel):
    job_id: str
    keyword: str
    expected_count: int
    succeeded_count: int
    artifact_id: int
    collected_at: datetime
    items: list[SearchNoteRead]


class _DaemonSerialWorker:
    """One daemon queue; a bounded adapter timeout prevents process-exit capture."""

    def __init__(self) -> None:
        self._queue: Queue[tuple[Future[Any], Callable[..., Any], tuple[Any, ...]] | None] = Queue()
        self._lock = RLock()
        self._closed = False
        self._thread: Thread | None = None

    def submit(self, action: Callable[..., Any], *args: Any) -> Future[Any]:
        future: Future[Any] = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("XHS collection worker is closed.")
            if self._thread is None:
                self._thread = Thread(target=self._run, name="xhs-collection", daemon=True)
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

    def join(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(timeout, 0))
        return not thread.is_alive()

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


class XhsCollectionService:
    """Reserve jobs, serialize CLI reads, and atomically finalize trusted facts."""

    def __init__(
        self,
        *,
        database: Database,
        job_service: JobService,
        adapter: Any,
        runtime_dir: Path,
        submitter: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.job_service = job_service
        self.adapter = adapter
        self.runtime_dir = runtime_dir.resolve()
        self.clock = clock or (lambda: datetime.now(UTC).replace(tzinfo=None))
        for job_type in XHS_RESERVED_JOB_TYPES:
            self.job_service.recover_interrupted_workers(job_type=job_type)
        self._worker = _DaemonSerialWorker() if submitter is None else None
        self._submitter = submitter or self._worker.submit
        self._lock = RLock()
        self._accepting = True
        self._futures: dict[str, Future[Any]] = {}

    def submit_account(self, user_id: str, expected_note_count: int) -> Job:
        if not _SAFE_SUBJECT.fullmatch(user_id):
            raise ValueError("user_id must be a safe platform identifier.")
        return self._submit(
            job_type=ACCOUNT_COLLECTION_JOB_TYPE,
            input_data={"user_id": user_id, "expected_note_count": expected_note_count},
            expected_count=expected_note_count,
        )

    def submit_search(self, keyword: str, expected_count: int) -> Job:
        keyword = keyword.strip()
        if not keyword or len(keyword) > 500 or keyword.startswith("-") or any(ord(c) < 32 for c in keyword):
            raise ValueError("keyword must be a safe non-empty search term.")
        return self._submit(
            job_type=SEARCH_COLLECTION_JOB_TYPE,
            input_data={"keyword": keyword, "expected_count": expected_count},
            expected_count=expected_count,
        )

    def _submit(self, *, job_type: str, input_data: dict[str, Any], expected_count: int) -> Job:
        if isinstance(expected_count, bool) or not isinstance(expected_count, int) or not 0 <= expected_count <= 1000:
            raise ValueError("expected count must be an integer from 0 through 1000.")
        with self._lock:
            if not self._accepting:
                raise CollectionServiceClosed("XHS collection service is closed.")
            job = self.job_service.create(
                job_type=job_type,
                input_data=input_data,
                progress_total=expected_count,
                current_stage="xhs_collection_reserved",
            )
            try:
                submitted = self._submitter(self.execute, job.id)
            except Exception:
                self._fail_scheduling(job.id)
                raise
            if isinstance(submitted, Future):
                self._futures[job.id] = submitted
                submitted.add_done_callback(lambda _future, job_id=job.id: self._finished(job_id))
            return job

    def execute(self, job_id: str) -> Job | None:
        try:
            self.job_service.claim(job_id)
        except InvalidJobTransition:
            return None
        if self._shutdown_requested():
            self._cancel_running(job_id)
            return None
        job = self.job_service.get(job_id)
        try:
            if job.type == ACCOUNT_COLLECTION_JOB_TYPE:
                expected_notes = int(job.input["expected_note_count"])
                request = CollectionRequest(
                    capability="fetch_account",
                    parameters={"user_id": job.input["user_id"], "job_id": job_id},
                    expected_count=expected_notes + 1,
                )
                result = self.adapter.fetch_account(request)
            elif job.type == SEARCH_COLLECTION_JOB_TYPE:
                expected_notes = int(job.input["expected_count"])
                request = CollectionRequest(
                    capability="search_notes",
                    parameters={"keyword": job.input["keyword"], "job_id": job_id},
                    expected_count=expected_notes,
                )
                result = self.adapter.search_notes(request)
            else:
                raise ValueError("Unsupported XHS reserved job type.")
            result = _redacted_result(result)
        except Exception as error:
            if self._shutdown_requested():
                return None
            return self._finalize_failure(job_id, category="xhs_collection_failed", error_type=type(error).__name__)
        if self._shutdown_requested() or self.job_service.get(job_id).state is not JobState.running:
            return None
        try:
            return self._finalize_result(job, result)
        except Exception as error:
            if self._committed_result(job.id):
                return self.job_service.get(job.id)
            return self._finalize_failure(job.id, category="xhs_collection_finalization_failed", error_type=type(error).__name__)
        finally:
            self._finished(job_id)

    def get_profile(self, user_id: str) -> AccountProfileRead:
        with self.database.session() as session:
            record = session.get(XhsAccountProfileRecord, user_id)
            if record is None:
                raise CollectionFactNotFound(f"Account profile {user_id} does not exist.")
            return AccountProfileRead(
                user_id=record.user_id, source_url=record.source_url,
                nickname=record.nickname, bio=record.bio,
                public_stats=dict(record.public_stats_json),
                collection_job_id=record.collection_job_id,
                collection_artifact_id=record.collection_artifact_id,
                collected_at=record.collected_at,
            )

    def list_account_notes(self, user_id: str) -> list[AccountNoteRead]:
        with self.database.session() as session:
            if session.get(XhsAccountProfileRecord, user_id) is None:
                raise CollectionFactNotFound(f"Account profile {user_id} does not exist.")
            records = session.scalars(
                select(XhsAccountNoteRecord)
                .where(XhsAccountNoteRecord.user_id == user_id)
                .order_by(XhsAccountNoteRecord.id)
            ).all()
            return [AccountNoteRead(
                note_id=row.note_id, user_id=row.user_id, source_url=row.source_url,
                title=row.title, summary=row.summary, published_at=row.published_at,
                public_interactions=dict(row.public_interactions_json),
                collection_job_id=row.collection_job_id,
                collection_artifact_id=row.collection_artifact_id,
                collected_at=row.collected_at,
            ) for row in records]

    def get_search_results(self, job_id: str) -> SearchResultsRead:
        job = self.job_service.get(job_id)
        if job.type != SEARCH_COLLECTION_JOB_TYPE or job.state is not JobState.succeeded:
            raise CollectionFactNotFound(f"Search results for job {job_id} do not exist.")
        artifacts = [artifact for artifact in job.artifacts if artifact.kind == SEARCH_COLLECTION_ARTIFACT_KIND]
        if len(artifacts) != 1 or artifacts[0].producer != ACCOUNT_COLLECTION_ARTIFACT_PRODUCER:
            raise CollectionFactNotFound(f"Search results for job {job_id} do not exist.")
        artifact = artifacts[0]
        try:
            absolute = (self.runtime_dir / artifact.path).resolve()
            absolute.relative_to(self.runtime_dir)
            encoded = absolute.read_bytes()
            if (
                len(encoded) != artifact.metadata["size_bytes"]
                or hashlib.sha256(encoded).hexdigest() != artifact.metadata["sha256"]
            ):
                raise ValueError("artifact identity mismatch")
            payload = json.loads(encoded.decode("utf-8"))
            if payload["job_id"] != job.id or payload["job_type"] != SEARCH_COLLECTION_JOB_TYPE:
                raise ValueError("artifact binding mismatch")
            result = CollectionResult.model_validate(payload["result"])
            if (
                not _is_exact(result, expected_count=int(job.input["expected_count"]))
                or any(item.kind != "note" for item in result.items)
            ):
                raise ValueError("search facts are not exact")
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise CollectionFactNotFound(
                f"Search results for job {job_id} do not exist."
            ) from error
        items = [_search_read(item) for item in result.items if item.kind == "note"]
        return SearchResultsRead(
            job_id=job.id, keyword=str(job.input["keyword"]),
            expected_count=int(job.input["expected_count"]),
            succeeded_count=result.succeeded_count,
            artifact_id=int(artifact.metadata["artifact_id"]),
            collected_at=datetime.fromisoformat(payload["collected_at"]), items=items,
        )

    def close(self) -> bool:
        with self._lock:
            if self._accepting:
                self._accepting = False
                futures = tuple(self._futures.values())
            else:
                futures = tuple(self._futures.values())
        for future in futures:
            future.cancel()
        for job in self.job_service.list():
            if job.type not in XHS_RESERVED_JOB_TYPES:
                continue
            if job.state in {JobState.queued, JobState.running}:
                self._cancel_running(job.id)
        if self._worker is None:
            return not any(not future.done() for future in futures)
        self._worker.close()
        return self._worker.join(0.25)

    def wait_for_idle(self, *, timeout: float) -> bool:
        if self._worker is not None:
            return self._worker.join(timeout)
        return not any(not future.done() for future in self._futures.values())

    def _finalize_result(self, job: Job, result: CollectionResult) -> Job | None:
        collected_at = self.clock()
        encoded = _result_bytes(job, result, collected_at)
        relative, digest = self._write_artifact(job.id, encoded)
        artifact_kind = (
            ACCOUNT_COLLECTION_ARTIFACT_KIND
            if job.type == ACCOUNT_COLLECTION_JOB_TYPE
            else SEARCH_COLLECTION_ARTIFACT_KIND
        )
        exact = _is_exact(result, expected_count=(job.progress_total or 0) + (1 if job.type == ACCOUNT_COLLECTION_JOB_TYPE else 0))
        target_state = JobState.succeeded if exact else (
            JobState.failed if result.status == "failed" else JobState.needs_human
        )
        progress = _note_success_count(result, account=job.type == ACCOUNT_COLLECTION_JOB_TYPE)
        metadata = {
            "sha256": digest, "size_bytes": len(encoded),
            "source": "xhs-cli", "job_id": job.id,
            "capability": (
                "fetch_account" if job.type == ACCOUNT_COLLECTION_JOB_TYPE else "search_notes"
            ),
            "expected_note_count": job.progress_total,
            "expected_item_count": result.expected_count,
            "expected_count": result.expected_count,
            "succeeded_note_count": progress,
            "succeeded_item_count": result.succeeded_count,
            "succeeded_count": result.succeeded_count,
            "observed_count": result.observed_count, "rejected_count": len(result.rejected_items),
            "missing_count": len(result.missing_items), "overflow_count": result.overflow_count,
            "complete": exact,
        }
        if job.type == ACCOUNT_COLLECTION_JOB_TYPE:
            metadata["user_id"] = job.input["user_id"]
        else:
            metadata["keyword"] = job.input["keyword"]
        try:
            with self.database.session() as session:
                record = session.get(JobRecord, job.id)
                if record is None or JobState(record.state) is not JobState.running:
                    session.rollback()
                    return None
                artifact = JobArtifactRecord(
                    job_id=job.id, kind=artifact_kind,
                    producer=ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
                    path=relative.as_posix(), metadata_json=metadata, created_at=collected_at,
                )
                session.add(artifact)
                session.flush()
                artifact.metadata_json = {**metadata, "artifact_id": artifact.id}
                if job.type == ACCOUNT_COLLECTION_JOB_TYPE and exact:
                    persist_exact_account_result(
                        session, result=result,
                        binding=AccountEvidenceBinding(
                            collection_job_id=job.id,
                            collection_artifact_id=artifact.id,
                            collected_at=collected_at,
                        ),
                    )
                now = self.clock()
                changed = session.execute(
                    update(JobRecord)
                    .where(JobRecord.id == job.id, JobRecord.state == JobState.running.value)
                    .values(
                        state=target_state.value, progress_current=progress,
                        progress_total=job.progress_total,
                        current_stage=("xhs_collection_complete" if target_state is JobState.succeeded else "xhs_collection_incomplete"),
                        error_category=None if target_state is JobState.succeeded else (result.detail or result.status),
                        lease_expires_at=None, completed_at=now if target_state in {JobState.succeeded, JobState.failed} else None,
                        updated_at=now,
                    )
                )
                if changed.rowcount != 1:
                    session.rollback()
                    return None
                session.commit()
        except Exception:
            if self._committed_result(job.id, digest=digest):
                return self.job_service.get(job.id)
            raise
        return self.job_service.get(job.id)

    def _finalize_failure(self, job_id: str, *, category: str, error_type: str) -> Job | None:
        job = self.job_service.get(job_id)
        payload = {
            "schema_version": 1, "job_id": job_id, "job_type": job.type,
            "status": "failed", "error_category": category, "error_type": error_type,
            "collected_at": self.clock().isoformat(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        relative, digest = self._write_artifact(job_id, encoded)
        kind = ACCOUNT_COLLECTION_ARTIFACT_KIND if job.type == ACCOUNT_COLLECTION_JOB_TYPE else SEARCH_COLLECTION_ARTIFACT_KIND
        now = self.clock()
        with self.database.session() as session:
            record = session.get(JobRecord, job_id)
            if record is None or JobState(record.state) is not JobState.running:
                session.rollback()
                return None
            artifact = JobArtifactRecord(
                job_id=job_id, kind=kind, producer=ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
                path=relative.as_posix(),
                metadata_json={
                    "sha256": digest, "size_bytes": len(encoded),
                    "source": "xhs-cli", "job_id": job_id,
                    "capability": (
                        "fetch_account" if job.type == ACCOUNT_COLLECTION_JOB_TYPE else "search_notes"
                    ),
                    "error_category": category,
                },
                created_at=now,
            )
            session.add(artifact)
            session.flush()
            artifact.metadata_json = {**artifact.metadata_json, "artifact_id": artifact.id}
            changed = session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id, JobRecord.state == JobState.running.value)
                .values(
                    state=JobState.failed.value, current_stage="xhs_collection_failed",
                    error_category=category, lease_expires_at=None,
                    completed_at=now, updated_at=now,
                )
            )
            if changed.rowcount != 1:
                session.rollback()
                return None
            session.commit()
        return self.job_service.get(job_id)

    def _write_artifact(self, job_id: str, encoded: bytes) -> tuple[Path, str]:
        relative = Path("evidence") / "xhs" / f"{job_id}.json"
        absolute = (self.runtime_dir / relative).resolve()
        absolute.relative_to(self.runtime_dir)
        absolute.parent.mkdir(parents=True, exist_ok=True)
        temporary = absolute.with_suffix(".json.tmp")
        temporary.write_bytes(encoded)
        temporary.replace(absolute)
        return relative, hashlib.sha256(encoded).hexdigest()

    def _committed_result(self, job_id: str, *, digest: str | None = None) -> bool:
        try:
            job = self.job_service.get(job_id)
        except Exception:
            return False
        if job.state is not JobState.succeeded or len(job.artifacts) != 1:
            return False
        return digest is None or job.artifacts[0].metadata.get("sha256") == digest

    def _fail_scheduling(self, job_id: str) -> None:
        try:
            self.job_service.claim(job_id)
            self.job_service.transition(
                job_id, JobState.failed, current_stage="xhs_collection_failed",
                error_category="xhs_collection_schedule_failed",
            )
        except Exception:
            return

    def _cancel_running(self, job_id: str) -> None:
        try:
            job = self.job_service.get(job_id)
            if job.state in {JobState.queued, JobState.running}:
                self.job_service.transition(
                    job_id, JobState.cancelled,
                    current_stage="worker_shutdown_cancelled",
                    error_category="worker_shutdown_cancelled",
                )
        except Exception:
            return

    def _shutdown_requested(self) -> bool:
        with self._lock:
            return not self._accepting

    def _finished(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)


def _result_bytes(job: Job, result: CollectionResult, collected_at: datetime) -> bytes:
    payload = {
        "schema_version": 1, "job_id": job.id, "job_type": job.type,
        "collected_at": collected_at.isoformat(), "result": result.model_dump(mode="json"),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _is_exact(result: CollectionResult, *, expected_count: int) -> bool:
    return (
        result.status == "succeeded" and result.complete and result.expected_count_known
        and result.expected_count == expected_count and result.succeeded_count == expected_count
        and result.observed_count == expected_count and result.raw_observation_count == expected_count
        and result.duplicate_observation_count == 0 and not result.rejected_items
        and not result.missing_items and result.overflow_count == 0
    )


def _note_success_count(result: CollectionResult, *, account: bool) -> int:
    return sum(item.kind == "note" for item in result.items) if account else result.succeeded_count


def _search_read(item: CollectionItem) -> SearchNoteRead:
    data = item.data
    note_id = data.get("note_id")
    if not isinstance(note_id, str) or not note_id:
        note_id = item.id.removeprefix("note:")
    return SearchNoteRead(
        note_id=note_id, source_url=str(item.source_url),
        title=data.get("title") if isinstance(data.get("title"), str) else None,
        summary=next((data[name] for name in ("summary", "description", "desc") if isinstance(data.get(name), str)), None),
        user_id=next((data[name] for name in ("user_id", "userId") if isinstance(data.get(name), str)), None),
    )


def _redacted_result(result: CollectionResult) -> CollectionResult:
    return CollectionResult.model_validate(_redact_sensitive(result.model_dump(mode="python")))


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _SENSITIVE_KEY.search(str(key)) else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    return value
