"""Durable orchestration for read-only XHS account and note collection."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from queue import Empty, Queue
from threading import Condition, RLock, Thread
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text, update

from backend.app.adapters.contracts import CollectionItem, CollectionRequest, CollectionResult
from backend.app.db import Database
from backend.app.features.xhs.constants import (
    ACCOUNT_COLLECTION_ARTIFACT_KIND,
    ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
    ACCOUNT_COLLECTION_JOB_TYPE,
)
from backend.app.features.xhs.models import (
    XhsAccountNoteRecord,
    XhsAccountProfileRecord,
    XhsArtifactPromotionJournalRecord,
)
from backend.app.features.xhs.ownership import OwnerIdentityError, canonical_owner_id
from backend.app.features.xhs.redaction import redact_credentials
from backend.app.features.xhs.schemas import AccountEvidenceBinding, persist_exact_account_result
from backend.app.features.xhs.staging_cleanup import (
    TrustedXhsArtifactStore,
    UnsafeXhsArtifactStore,
    XhsArtifactIdentity,
    discard_xhs_staging_file,
)
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
DEFAULT_XHS_ARTIFACT_MAX_BYTES = 5 * 1024 * 1024
_CLOSE_BUDGET_SECONDS = 0.25
_JOURNAL_READ_BUDGET_SECONDS = 0.05
_JOURNAL_RECONCILE_LOCK = RLock()
_JOURNAL_LEASE_SQL = "datetime('now','+5 minutes')"

# Narrow crash-injection hook used only by local lifecycle tests.
_artifact_promotion_after_atomic_hook: Callable[[], None] | None = None


class CollectionServiceClosed(RuntimeError):
    """Admission has closed and no new durable reservation was created."""


class CollectionFactNotFound(LookupError):
    """A requested normalized collection fact does not exist."""


class CollectionResultTooLarge(ValueError):
    """The transformed collection artifact crossed its configured hard cap."""


class ArtifactCommitUnknown(RuntimeError):
    """The final commit acknowledgement cannot be classified safely yet."""


class _CommitOutcome(str, Enum):
    committed = "committed"
    rolled_back = "rolled_back"
    unknown = "unknown"


@dataclass
class _StagedArtifact:
    journal_id: str
    store: TrustedXhsArtifactStore
    stage_name: str
    final_name: str
    relative_path: Path
    digest: str
    size_bytes: int
    identity: XhsArtifactIdentity
    promoted: bool = False


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
        max_artifact_bytes: int = DEFAULT_XHS_ARTIFACT_MAX_BYTES,
    ) -> None:
        if not 1024 <= max_artifact_bytes <= 20 * 1024 * 1024:
            raise ValueError("XHS artifact cap must be between 1024 and 20 MiB.")
        self.database = database
        self.job_service = job_service
        self.adapter = adapter
        self.runtime_dir = Path(os.path.abspath(runtime_dir))
        self.clock = clock or (lambda: datetime.now(UTC).replace(tzinfo=None))
        self._max_artifact_bytes = max_artifact_bytes
        self._journal_owner = str(uuid4())
        self._reconcile_artifact_promotions()
        for job_type in XHS_RESERVED_JOB_TYPES:
            self.job_service.recover_interrupted_workers(
                job_type=job_type,
                protect_active_xhs_finalizers=True,
            )
        self._worker = _DaemonSerialWorker() if submitter is None else None
        self._submitter = submitter or self._worker.submit
        self._lock = RLock()
        self._admission_condition = Condition(self._lock)
        self._accepting = True
        self._admission_generation = 0
        self._active_admissions = 0
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
        with self._admission_condition:
            if not self._accepting:
                raise CollectionServiceClosed("XHS collection service is closed.")
            generation = self._admission_generation
            self._active_admissions += 1
        try:
            job = self.job_service.create(
                job_type=job_type,
                input_data=input_data,
                progress_total=expected_count,
                current_stage="xhs_collection_reserved",
            )
            with self._admission_condition:
                admitted = (
                    self._accepting
                    and generation == self._admission_generation
                )
            if not admitted:
                self._cancel_running(job.id)
                raise CollectionServiceClosed("XHS collection service is closed.")
            try:
                submitted = self._submitter(self.execute, job.id)
            except Exception:
                self._fail_scheduling(job.id)
                raise
            if isinstance(submitted, Future):
                with self._admission_condition:
                    self._futures[job.id] = submitted
                    submitted.add_done_callback(
                        lambda _future, job_id=job.id: self._finished(job_id)
                    )
            return job
        finally:
            with self._admission_condition:
                self._active_admissions -= 1
                self._admission_condition.notify_all()

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
            if job.type == SEARCH_COLLECTION_JOB_TYPE:
                _validate_search_owners(result)
        except Exception as error:
            if self._shutdown_requested():
                return None
            try:
                return self._finalize_failure(
                    job_id,
                    category="xhs_collection_failed",
                    error_type=type(error).__name__,
                )
            except ArtifactCommitUnknown:
                return None
        if self._shutdown_requested() or self.job_service.get(job_id).state is not JobState.running:
            return None
        try:
            return self._finalize_result(job, result)
        except CollectionResultTooLarge as error:
            return self._finalize_failure(
                job.id,
                category="xhs_collection_result_too_large",
                error_type=type(error).__name__,
            )
        except ArtifactCommitUnknown:
            return None
        except Exception as error:
            if self._committed_result(job.id):
                return self._read_committed_job(job.id)
            return self._finalize_failure(job.id, category="xhs_collection_finalization_failed", error_type=type(error).__name__)
        finally:
            self._finished(job_id)

    def get_profile(self, user_id: str) -> AccountProfileRead:
        try:
            with self.database.session() as session:
                record = session.get(XhsAccountProfileRecord, user_id)
                if record is None:
                    raise CollectionFactNotFound(
                        f"Account profile {user_id} does not exist."
                    )
                snapshot = AccountProfileRead(
                    user_id=record.user_id,
                    source_url=record.source_url,
                    nickname=record.nickname,
                    bio=record.bio,
                    public_stats=dict(record.public_stats_json),
                    collection_job_id=record.collection_job_id,
                    collection_artifact_id=record.collection_artifact_id,
                    collected_at=record.collected_at,
                )
            self._read_trusted_collection_result(
                job_id=snapshot.collection_job_id,
                artifact_id=snapshot.collection_artifact_id,
                expected_job_type=ACCOUNT_COLLECTION_JOB_TYPE,
                expected_artifact_kind=ACCOUNT_COLLECTION_ARTIFACT_KIND,
                binding_name="user_id",
                binding_value=user_id,
            )
            return snapshot
        except CollectionFactNotFound:
            raise
        except Exception as error:
            raise CollectionFactNotFound(
                f"Account profile {user_id} does not exist."
            ) from error

    def list_account_notes(self, user_id: str) -> list[AccountNoteRead]:
        try:
            with self.database.session() as session:
                profile = session.get(XhsAccountProfileRecord, user_id)
                if profile is None:
                    raise CollectionFactNotFound(
                        f"Account profile {user_id} does not exist."
                    )
                records = session.scalars(
                    select(XhsAccountNoteRecord)
                    .where(XhsAccountNoteRecord.user_id == user_id)
                    .order_by(XhsAccountNoteRecord.id)
                ).all()
                if any(
                    row.collection_job_id != profile.collection_job_id
                    or row.collection_artifact_id != profile.collection_artifact_id
                    for row in records
                ):
                    raise CollectionFactNotFound(
                        f"Account notes for {user_id} do not exist."
                    )
                profile_job_id = profile.collection_job_id
                profile_artifact_id = profile.collection_artifact_id
                snapshots = [
                    AccountNoteRead(
                        note_id=row.note_id,
                        user_id=row.user_id,
                        source_url=row.source_url,
                        title=row.title,
                        summary=row.summary,
                        published_at=row.published_at,
                        public_interactions=dict(row.public_interactions_json),
                        collection_job_id=row.collection_job_id,
                        collection_artifact_id=row.collection_artifact_id,
                        collected_at=row.collected_at,
                    )
                    for row in records
                ]
            self._read_trusted_collection_result(
                job_id=profile_job_id,
                artifact_id=profile_artifact_id,
                expected_job_type=ACCOUNT_COLLECTION_JOB_TYPE,
                expected_artifact_kind=ACCOUNT_COLLECTION_ARTIFACT_KIND,
                binding_name="user_id",
                binding_value=user_id,
            )
            return snapshots
        except CollectionFactNotFound:
            raise
        except Exception as error:
            raise CollectionFactNotFound(
                f"Account notes for {user_id} do not exist."
            ) from error

    def get_search_results(self, job_id: str) -> SearchResultsRead:
        try:
            with self.database.session() as session:
                job_record = session.get(JobRecord, job_id)
                if job_record is None:
                    raise CollectionFactNotFound(
                        f"Search results for job {job_id} do not exist."
                    )
                keyword = job_record.input_data.get("keyword")
                artifacts = session.scalars(
                    select(JobArtifactRecord).where(JobArtifactRecord.job_id == job_id)
                ).all()
                if len(artifacts) != 1 or not isinstance(keyword, str):
                    raise CollectionFactNotFound(
                        f"Search results for job {job_id} do not exist."
                    )
                artifact_id = artifacts[0].id
            job_input, artifact_metadata, payload, result = (
                self._read_trusted_collection_result(
                    job_id=job_id,
                    artifact_id=artifact_id,
                    expected_job_type=SEARCH_COLLECTION_JOB_TYPE,
                    expected_artifact_kind=SEARCH_COLLECTION_ARTIFACT_KIND,
                    binding_name="keyword",
                    binding_value=keyword,
                )
            )
            if (
                not _is_exact(result, expected_count=int(job_input["expected_count"]))
                or any(item.kind != "note" for item in result.items)
            ):
                raise ValueError("search facts are not exact")
            _validate_search_owners(result)
        except CollectionFactNotFound:
            raise
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise CollectionFactNotFound(
                f"Search results for job {job_id} do not exist."
            ) from error
        items = [_search_read(item) for item in result.items if item.kind == "note"]
        return SearchResultsRead(
            job_id=job_id, keyword=str(job_input["keyword"]),
            expected_count=int(job_input["expected_count"]),
            succeeded_count=result.succeeded_count,
            artifact_id=int(artifact_metadata["artifact_id"]),
            collected_at=datetime.fromisoformat(payload["collected_at"]), items=items,
        )

    def _read_trusted_collection_result(
        self,
        *,
        job_id: str,
        artifact_id: int,
        expected_job_type: str,
        expected_artifact_kind: str,
        binding_name: str,
        binding_value: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], CollectionResult]:
        """Read one stable succeeded artifact through the shared provenance gate."""

        with self.database.session() as session:
            job = session.get(JobRecord, job_id)
            artifacts = session.scalars(
                select(JobArtifactRecord).where(JobArtifactRecord.job_id == job_id)
            ).all()
            journals = session.scalars(
                select(XhsArtifactPromotionJournalRecord).where(
                    XhsArtifactPromotionJournalRecord.job_id == job_id,
                    XhsArtifactPromotionJournalRecord.state == "completed",
                    XhsArtifactPromotionJournalRecord.resolution == "committed",
                )
            ).all()
            if job is None or len(artifacts) != 1 or len(journals) != 1:
                raise UnsafeXhsArtifactStore("XHS provenance is not unique.")
            artifact = artifacts[0]
            journal = journals[0]
            metadata = (
                dict(artifact.metadata_json)
                if isinstance(artifact.metadata_json, dict)
                else {}
            )
            job_input = (
                dict(job.input_data) if isinstance(job.input_data, dict) else {}
            )
            metadata_artifact_id = metadata.get("artifact_id")
            metadata_size = metadata.get("size_bytes")
            if (
                JobState(job.state) is not JobState.succeeded
                or job.type != expected_job_type
                or artifact.id != artifact_id
                or artifact.kind != expected_artifact_kind
                or artifact.producer != ACCOUNT_COLLECTION_ARTIFACT_PRODUCER
                or journal.target_state != JobState.succeeded.value
                or journal.owner_token is not None
                or journal.recovery_lease_expires_at is not None
                or journal.artifact_id != artifact.id
                or journal.artifact_kind != artifact.kind
                or journal.producer != artifact.producer
                or journal.final_path != artifact.path
                or not isinstance(metadata_artifact_id, int)
                or isinstance(metadata_artifact_id, bool)
                or metadata_artifact_id != artifact.id
                or metadata.get("job_id") != job_id
                or metadata.get("sha256") != journal.sha256
                or not isinstance(metadata_size, int)
                or isinstance(metadata_size, bool)
                or metadata_size != journal.size_bytes
                or job_input.get(binding_name) != binding_value
                or metadata.get(binding_name) != binding_value
                or journal.file_dev is None
                or journal.file_ino is None
                or journal.file_mtime_ns is None
            ):
                raise UnsafeXhsArtifactStore("XHS provenance binding changed.")
            final_name = Path(journal.final_path).name
            identity = XhsArtifactIdentity(
                journal.file_dev,
                journal.file_ino,
                journal.size_bytes,
                journal.file_mtime_ns,
            )
            digest = journal.sha256
            size = journal.size_bytes
        with TrustedXhsArtifactStore(
            self.runtime_dir,
            max_bytes=self._max_artifact_bytes,
        ) as store:
            encoded = store.read_final(final_name, identity)
        if len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest:
            raise UnsafeXhsArtifactStore("XHS formal evidence changed.")
        payload = json.loads(encoded.decode("utf-8"))
        if payload.get("job_id") != job_id or payload.get("job_type") != expected_job_type:
            raise UnsafeXhsArtifactStore("XHS formal evidence binding changed.")
        result = CollectionResult.model_validate(payload.get("result"))
        return job_input, metadata, payload, result

    def close(self) -> bool:
        deadline = monotonic() + _CLOSE_BUDGET_SECONDS
        with self._admission_condition:
            if self._accepting:
                self._accepting = False
                self._admission_generation += 1
            futures = tuple(self._futures.values())
        adapter_safe = True
        adapter_close = getattr(self.adapter, "close", None)
        if callable(adapter_close):
            remaining = max(deadline - monotonic(), 0)
            try:
                adapter_safe = bool(adapter_close(timeout=remaining))
            except (OSError, TypeError, ValueError):
                adapter_safe = False
        for future in futures:
            future.cancel()
        for job in self.job_service.list():
            if job.type not in XHS_RESERVED_JOB_TYPES:
                continue
            if job.state in {JobState.queued, JobState.running}:
                self._cancel_running(job.id)
        worker_safe = True
        if self._worker is not None:
            self._worker.close()
            worker_safe = self._worker.join(max(deadline - monotonic(), 0))
        with self._admission_condition:
            while self._active_admissions:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                self._admission_condition.wait(remaining)
            admissions_safe = self._active_admissions == 0
        futures_safe = all(future.done() for future in futures)
        return adapter_safe and worker_safe and admissions_safe and futures_safe

    def wait_for_idle(self, *, timeout: float) -> bool:
        if self._worker is not None:
            return self._worker.join(timeout)
        return not any(not future.done() for future in self._futures.values())

    def _finalize_result(self, job: Job, result: CollectionResult) -> Job | None:
        collected_at = self.clock()
        encoded = _result_bytes(
            job,
            result,
            collected_at,
            max_bytes=self._max_artifact_bytes,
        )
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
        staged = self._stage_artifact(
            job.id,
            encoded,
            artifact_kind=artifact_kind,
            target_state=target_state,
        )
        metadata = {
            "sha256": staged.digest, "size_bytes": staged.size_bytes,
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

        def persist_facts(session: Any, artifact_id: int) -> None:
            if job.type == ACCOUNT_COLLECTION_JOB_TYPE and exact:
                persist_exact_account_result(
                    session,
                    result=result,
                    binding=AccountEvidenceBinding(
                        collection_job_id=job.id,
                        collection_artifact_id=artifact_id,
                        collected_at=collected_at,
                    ),
                )

        try:
            return self._commit_staged_artifact(
                job=job,
                staged=staged,
                artifact_kind=artifact_kind,
                metadata=metadata,
                created_at=collected_at,
                target_state=target_state,
                progress=progress,
                current_stage=(
                    "xhs_collection_complete"
                    if target_state is JobState.succeeded
                    else "xhs_collection_incomplete"
                ),
                error_category=(
                    None
                    if target_state is JobState.succeeded
                    else (result.detail or result.status)
                ),
                persist_facts=persist_facts,
            )
        finally:
            staged.store.close()

    def _finalize_failure(self, job_id: str, *, category: str, error_type: str) -> Job | None:
        job = self.job_service.get(job_id)
        if job.state is not JobState.running:
            return job
        payload = {
            "schema_version": 1, "job_id": job_id, "job_type": job.type,
            "status": "failed", "error_category": category, "error_type": error_type,
            "collected_at": self.clock().isoformat(),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        kind = (
            ACCOUNT_COLLECTION_ARTIFACT_KIND
            if job.type == ACCOUNT_COLLECTION_JOB_TYPE
            else SEARCH_COLLECTION_ARTIFACT_KIND
        )
        now = self.clock()
        staged = self._stage_artifact(
            job_id,
            encoded,
            suffix="-failure",
            artifact_kind=kind,
            target_state=JobState.failed,
        )
        metadata = {
            "sha256": staged.digest,
            "size_bytes": staged.size_bytes,
            "source": "xhs-cli",
            "job_id": job_id,
            "capability": (
                "fetch_account"
                if job.type == ACCOUNT_COLLECTION_JOB_TYPE
                else "search_notes"
            ),
            "error_category": category,
        }
        if job.type == ACCOUNT_COLLECTION_JOB_TYPE:
            metadata["user_id"] = job.input["user_id"]
        else:
            metadata["keyword"] = job.input["keyword"]
        try:
            return self._commit_staged_artifact(
                job=job,
                staged=staged,
                artifact_kind=kind,
                metadata=metadata,
                created_at=now,
                target_state=JobState.failed,
                progress=job.progress_current,
                current_stage="xhs_collection_failed",
                error_category=category,
                persist_facts=None,
            )
        finally:
            staged.store.close()

    def _stage_artifact(
        self,
        job_id: str,
        encoded: bytes,
        *,
        artifact_kind: str,
        target_state: JobState,
        suffix: str = "",
    ) -> _StagedArtifact:
        journal_id = str(uuid4())
        journal_token = journal_id.replace("-", "")
        stage_name = f"{job_id}-{journal_token}{suffix}.stage"
        final_name = f"{job_id}{suffix}.json"
        relative = Path("evidence") / "xhs" / f"{job_id}{suffix}.json"
        stage_relative = Path("evidence") / "xhs" / ".staging" / stage_name
        digest = hashlib.sha256(encoded).hexdigest()
        size_bytes = len(encoded)
        with self.database.session() as session:
            session.execute(text(f"""
                INSERT INTO xhs_artifact_promotion_journal (
                    id, job_id, artifact_kind, producer, stage_path, final_path,
                    sha256, size_bytes, file_dev, file_ino, file_mtime_ns,
                    target_state, state, resolution, artifact_id, created_at,
                    updated_at, completed_at, owner_token,
                    recovery_lease_expires_at
                ) VALUES (
                    :id, :job_id, :artifact_kind, :producer, :stage_path,
                    :final_path, :sha256, :size_bytes, NULL, NULL, NULL,
                    :target_state, 'allocating', NULL, NULL, CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP, NULL, :owner_token,
                    {_JOURNAL_LEASE_SQL}
                )
            """), {
                "id": journal_id,
                "job_id": job_id,
                "artifact_kind": artifact_kind,
                "producer": ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
                "stage_path": stage_relative.as_posix(),
                "final_path": relative.as_posix(),
                "sha256": digest,
                "size_bytes": size_bytes,
                "target_state": target_state.value,
                "owner_token": self._journal_owner,
            })
            session.commit()
        try:
            store = TrustedXhsArtifactStore(
                self.runtime_dir,
                max_bytes=self._max_artifact_bytes,
            )
        except Exception:
            self._complete_stage_creation_failure(
                journal_id,
                inconsistent=False,
            )
            raise
        preserve_stage = False
        failure_resolved = False
        try:
            try:
                identity = store.create_stage(stage_name, encoded)
            except Exception:
                deleted = store.discard_owned_stage(stage_name)
                self._complete_stage_creation_failure(
                    journal_id,
                    inconsistent=not deleted,
                )
                failure_resolved = True
                raise
            staged = _StagedArtifact(
                journal_id=journal_id,
                store=store,
                stage_name=stage_name,
                final_name=final_name,
                relative_path=relative,
                digest=digest,
                size_bytes=size_bytes,
                identity=identity,
            )
            try:
                with self.database.session() as session:
                    changed = session.execute(text(f"""
                        UPDATE xhs_artifact_promotion_journal
                        SET state='prepared', file_dev=:file_dev,
                            file_ino=:file_ino, file_mtime_ns=:file_mtime_ns,
                            updated_at=CURRENT_TIMESTAMP,
                            recovery_lease_expires_at={_JOURNAL_LEASE_SQL}
                        WHERE id=:id AND state='allocating'
                        AND owner_token=:owner_token
                        AND recovery_lease_expires_at > CURRENT_TIMESTAMP
                    """), {
                        "id": journal_id,
                        "owner_token": self._journal_owner,
                        "file_dev": identity.file_dev,
                        "file_ino": identity.file_ino,
                        "file_mtime_ns": identity.file_mtime_ns,
                    })
                    if changed.rowcount != 1:
                        raise ArtifactCommitUnknown(
                            "artifact_journal_allocation_lease_lost"
                        )
                    session.commit()
            except Exception as error:
                outcome = self._prepared_journal_outcome(staged)
                if outcome is _CommitOutcome.committed:
                    return staged
                if outcome is _CommitOutcome.unknown:
                    preserve_stage = True
                    raise ArtifactCommitUnknown(
                        "artifact_journal_prepare_unknown"
                    ) from error
                deleted = store.discard_owned_stage(stage_name)
                self._complete_stage_creation_failure(
                    journal_id,
                    inconsistent=not deleted,
                )
                failure_resolved = True
                raise
            return staged
        except Exception:
            if not preserve_stage and not failure_resolved:
                # No pathname-based cleanup is attempted here.  A durable row
                # plus the store's exact held handle decides the failure state.
                deleted = store.discard_owned_stage(stage_name)
                self._complete_stage_creation_failure(
                    journal_id,
                    inconsistent=not deleted,
                )
            store.close()
            raise
        except BaseException:
            store.close()
            raise

    def _complete_stage_creation_failure(
        self,
        journal_id: str,
        *,
        inconsistent: bool,
    ) -> Job | None:
        resolution = "inconsistent" if inconsistent else "rolled_back"
        category = (
            "artifact_stage_create_inconsistent"
            if inconsistent
            else "artifact_stage_create_failed"
        )
        with self.database.session() as session:
            journal = session.get(XhsArtifactPromotionJournalRecord, journal_id)
            if journal is None:
                return None
            if journal.state != "completed":
                journal.state = "completed"
                journal.resolution = resolution
                journal.artifact_id = None
                journal.owner_token = None
                journal.recovery_lease_expires_at = None
                journal.updated_at = self.clock()
                journal.completed_at = journal.updated_at
            session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == journal.job_id,
                    JobRecord.state.in_((
                        JobState.queued.value,
                        JobState.running.value,
                    )),
                )
                .values(
                    state=JobState.needs_human.value,
                    current_stage="xhs_artifact_recovery_required",
                    error_category=category,
                    lease_expires_at=None,
                    completed_at=None,
                    updated_at=self.clock(),
                )
            )
            job_id = journal.job_id
            session.commit()
        return self._read_committed_job(job_id)

    def _probe_prepared_journal_outcome(
        self,
        staged: _StagedArtifact,
    ) -> _CommitOutcome:
        with self.database.session() as session:
            journal = session.get(
                XhsArtifactPromotionJournalRecord,
                staged.journal_id,
            )
            if journal is None:
                return _CommitOutcome.unknown
            if (
                journal.state == "prepared"
                and journal.job_id == staged.relative_path.stem.removesuffix("-failure")
                and journal.stage_path.endswith("/" + staged.stage_name)
                and journal.final_path == staged.relative_path.as_posix()
                and journal.sha256 == staged.digest
                and journal.size_bytes == staged.size_bytes
                and journal.file_dev == staged.identity.file_dev
                and journal.file_ino == staged.identity.file_ino
                and journal.file_mtime_ns == staged.identity.file_mtime_ns
                and journal.owner_token == self._journal_owner
            ):
                return _CommitOutcome.committed
            if (
                journal.state == "allocating"
                and journal.job_id
                == staged.relative_path.stem.removesuffix("-failure")
                and journal.stage_path.endswith("/" + staged.stage_name)
                and journal.final_path == staged.relative_path.as_posix()
                and journal.sha256 == staged.digest
                and journal.size_bytes == staged.size_bytes
                and journal.owner_token == self._journal_owner
            ):
                return _CommitOutcome.rolled_back
            return _CommitOutcome.unknown

    def _prepared_journal_outcome(
        self,
        staged: _StagedArtifact,
    ) -> _CommitOutcome:
        deadline = monotonic() + _JOURNAL_READ_BUDGET_SECONDS
        while True:
            try:
                return self._probe_prepared_journal_outcome(staged)
            except Exception:
                if monotonic() >= deadline:
                    return _CommitOutcome.unknown
                sleep(0.002)

    def _promote_staged_artifact(self, staged: _StagedArtifact) -> None:
        if not self._refresh_journal_lease(staged.journal_id):
            raise ArtifactCommitUnknown("artifact_journal_lease_lost")
        try:
            staged.store.promote(
                staged.stage_name,
                staged.final_name,
                staged.identity,
            )
            staged.promoted = True
            if _artifact_promotion_after_atomic_hook is not None:
                _artifact_promotion_after_atomic_hook()
        except Exception:
            final = staged.store.inspect_final(staged.final_name)
            stage = staged.store.inspect_stage(staged.stage_name)
            if (
                final.status == "trusted"
                and final.identity == staged.identity
                and stage.status == "missing"
            ):
                staged.promoted = True
            else:
                raise

    def _persist_promoted_journal(self, staged: _StagedArtifact) -> None:
        try:
            with self.database.session() as session:
                changed = session.execute(text(f"""
                    UPDATE xhs_artifact_promotion_journal
                    SET state='promoted', updated_at=CURRENT_TIMESTAMP,
                        recovery_lease_expires_at={_JOURNAL_LEASE_SQL}
                    WHERE id=:id AND state='prepared'
                    AND owner_token=:owner_token
                """), {
                    "id": staged.journal_id,
                    "owner_token": self._journal_owner,
                })
                if changed.rowcount != 1:
                    session.rollback()
                    if self._read_journal_state(staged.journal_id) == "promoted":
                        return
                    raise ArtifactCommitUnknown("artifact_journal_state_unknown")
                session.commit()
        except ArtifactCommitUnknown:
            raise
        except Exception as error:
            state = self._read_journal_state(staged.journal_id)
            if state == "promoted":
                return
            raise ArtifactCommitUnknown("artifact_journal_promotion_unknown") from error

    def _refresh_journal_lease(self, journal_id: str) -> bool:
        try:
            with self.database.session() as session:
                changed = session.execute(text(f"""
                    UPDATE xhs_artifact_promotion_journal
                    SET recovery_lease_expires_at={_JOURNAL_LEASE_SQL},
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=:id AND owner_token=:owner_token
                """), {
                    "id": journal_id,
                    "owner_token": self._journal_owner,
                })
                session.commit()
                return changed.rowcount == 1
        except Exception:
            return False

    def _claim_journal(self, journal_id: str) -> bool:
        """Acquire or renew one journal with a database-clock CAS."""

        try:
            with self.database.session() as session:
                changed = session.execute(text(f"""
                    UPDATE xhs_artifact_promotion_journal
                    SET owner_token=:owner_token,
                        recovery_lease_expires_at={_JOURNAL_LEASE_SQL},
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=:id AND (
                        owner_token=:owner_token OR owner_token IS NULL
                        OR recovery_lease_expires_at IS NULL
                        OR recovery_lease_expires_at <= CURRENT_TIMESTAMP
                    )
                """), {
                    "id": journal_id,
                    "owner_token": self._journal_owner,
                })
                session.commit()
                return changed.rowcount == 1
        except Exception:
            return False

    def _read_journal_state(self, journal_id: str) -> str | None:
        deadline = monotonic() + _JOURNAL_READ_BUDGET_SECONDS
        while True:
            try:
                with self.database.session() as session:
                    row = session.get(XhsArtifactPromotionJournalRecord, journal_id)
                    return None if row is None else row.state
            except Exception:
                if monotonic() >= deadline:
                    return None
                sleep(0.002)

    def _commit_staged_artifact(
        self,
        *,
        job: Job,
        staged: _StagedArtifact,
        artifact_kind: str,
        metadata: dict[str, Any],
        created_at: datetime,
        target_state: JobState,
        progress: int,
        current_stage: str,
        error_category: str | None,
        persist_facts: Callable[[Any, int], None] | None,
    ) -> Job | None:
        with self.database.session() as authorization:
            record = authorization.get(JobRecord, job.id)
            authorized = record is not None and JobState(record.state) is JobState.running
        if not authorized or not self._refresh_journal_lease(staged.journal_id):
            self._resolve_rolled_back(staged)
            return None
        with self._lock:
            if not self._accepting:
                self._cancel_running(job.id)
                self._resolve_rolled_back(staged)
                return None
            try:
                self._promote_staged_artifact(staged)
            except Exception:
                return self._resolve_rolled_back(staged)
        self._persist_promoted_journal(staged)
        if not self._refresh_journal_lease(staged.journal_id):
            raise ArtifactCommitUnknown("artifact_journal_lease_lost")

        try:
            with self.database.session() as session:
                record = session.get(JobRecord, job.id)
                journal = session.get(
                    XhsArtifactPromotionJournalRecord,
                    staged.journal_id,
                )
                if (
                    record is None
                    or JobState(record.state) is not JobState.running
                    or journal is None
                    or journal.state != "promoted"
                    or journal.owner_token != self._journal_owner
                ):
                    session.rollback()
                    self._resolve_rolled_back(staged)
                    return None
                artifact = JobArtifactRecord(
                    job_id=job.id,
                    kind=artifact_kind,
                    producer=ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
                    path=staged.relative_path.as_posix(),
                    metadata_json=metadata,
                    created_at=created_at,
                )
                session.add(artifact)
                session.flush()
                artifact.metadata_json = {**metadata, "artifact_id": artifact.id}
                if persist_facts is not None:
                    persist_facts(session, artifact.id)
                now = self.clock()
                with self._lock:
                    if not self._accepting:
                        session.rollback()
                        self._cancel_running(job.id)
                        self._resolve_rolled_back(staged)
                        return None
                    changed = session.execute(
                        update(JobRecord)
                        .where(
                            JobRecord.id == job.id,
                            JobRecord.state == JobState.running.value,
                        )
                        .values(
                            state=target_state.value,
                            progress_current=progress,
                            progress_total=job.progress_total,
                            current_stage=current_stage,
                            error_category=error_category,
                            lease_expires_at=None,
                            completed_at=(
                                now
                                if target_state in {JobState.succeeded, JobState.failed}
                                else None
                            ),
                            updated_at=now,
                        )
                    )
                    if changed.rowcount != 1:
                        session.rollback()
                        self._resolve_rolled_back(staged)
                        return None
                    journal.state = "completed"
                    journal.resolution = "committed"
                    journal.artifact_id = artifact.id
                    journal.owner_token = None
                    journal.recovery_lease_expires_at = None
                    journal.updated_at = now
                    journal.completed_at = now
                    session.commit()
        except Exception as error:
            outcome = self._commit_outcome(
                staged,
                expected_state=target_state,
            )
            if outcome is _CommitOutcome.committed:
                return self._read_committed_job(job.id)
            if outcome is _CommitOutcome.rolled_back:
                return self._resolve_rolled_back(staged)
            raise ArtifactCommitUnknown("artifact_commit_unknown") from error
        try:
            return self._read_committed_job(job.id)
        except Exception as error:
            raise ArtifactCommitUnknown("artifact_commit_unknown") from error

    def _probe_commit_outcome(
        self,
        staged: _StagedArtifact,
        *,
        expected_state: JobState,
    ) -> _CommitOutcome:
        with self.database.session() as session:
            journal = session.get(
                XhsArtifactPromotionJournalRecord,
                staged.journal_id,
            )
            job = session.get(JobRecord, staged.relative_path.stem.removesuffix("-failure"))
            artifacts = session.scalars(
                select(JobArtifactRecord).where(
                    JobArtifactRecord.job_id
                    == staged.relative_path.stem.removesuffix("-failure")
                )
            ).all()
        if journal is None or job is None:
            return _CommitOutcome.unknown
        if (
            journal.state == "completed"
            and journal.resolution == "committed"
            and journal.artifact_id is not None
            and journal.owner_token is None
            and journal.recovery_lease_expires_at is None
            and JobState(job.state) is expected_state
            and len(artifacts) == 1
            and artifacts[0].id == journal.artifact_id
            and artifacts[0].path == journal.final_path
            and dict(artifacts[0].metadata_json).get("sha256") == staged.digest
            and dict(artifacts[0].metadata_json).get("size_bytes") == staged.size_bytes
        ):
            encoded = staged.store.read_final(staged.final_name, staged.identity)
            return (
                _CommitOutcome.committed
                if hashlib.sha256(encoded).hexdigest() == staged.digest
                else _CommitOutcome.unknown
            )
        if (
            journal.state in {"prepared", "promoted"}
            and not artifacts
            and JobState(job.state) is not expected_state
        ) or (
            journal.state in {"prepared", "promoted"}
            and not artifacts
            and JobState(job.state) is JobState.running
        ):
            return _CommitOutcome.rolled_back
        return _CommitOutcome.unknown

    def _commit_outcome(
        self,
        staged: _StagedArtifact,
        *,
        expected_state: JobState,
    ) -> _CommitOutcome:
        deadline = monotonic() + _JOURNAL_READ_BUDGET_SECONDS
        while True:
            try:
                return self._probe_commit_outcome(
                    staged,
                    expected_state=expected_state,
                )
            except Exception:
                if monotonic() >= deadline:
                    return _CommitOutcome.unknown
                sleep(0.002)

    def _resolve_rolled_back(self, staged: _StagedArtifact) -> Job | None:
        if not self._refresh_journal_lease(staged.journal_id):
            return None
        if not staged.store.demote_and_discard(
            staged.final_name,
            staged.stage_name,
            staged.identity,
        ):
            return self._mark_artifact_inconsistent(staged.journal_id)
        now = self.clock()
        with self.database.session() as session:
            journal = session.get(XhsArtifactPromotionJournalRecord, staged.journal_id)
            if journal is None or journal.owner_token != self._journal_owner:
                return None
            if journal.state == "completed" and journal.resolution == "committed":
                session.rollback()
                return self._read_committed_job(journal.job_id)
            journal.state = "completed"
            journal.resolution = "rolled_back"
            journal.artifact_id = None
            journal.owner_token = None
            journal.recovery_lease_expires_at = None
            journal.updated_at = now
            journal.completed_at = now
            session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == journal.job_id,
                    JobRecord.state.in_((
                        JobState.queued.value,
                        JobState.running.value,
                    )),
                )
                .values(
                    state=JobState.needs_human.value,
                    current_stage="xhs_artifact_recovery_required",
                    error_category="artifact_commit_rolled_back",
                    lease_expires_at=None,
                    completed_at=None,
                    updated_at=now,
                )
            )
            job_id = journal.job_id
            session.commit()
        return self._read_committed_job(job_id)

    def _mark_artifact_inconsistent(self, journal_id: str) -> Job | None:
        if not self._claim_journal(journal_id):
            return None
        now = self.clock()
        with self.database.session() as session:
            journal = session.get(XhsArtifactPromotionJournalRecord, journal_id)
            if journal is None or journal.owner_token != self._journal_owner:
                return None
            journal.state = "completed"
            journal.resolution = "inconsistent"
            journal.owner_token = None
            journal.recovery_lease_expires_at = None
            journal.updated_at = now
            journal.completed_at = now
            session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == journal.job_id,
                    JobRecord.state != JobState.cancelled.value,
                )
                .values(
                    state=JobState.needs_human.value,
                    current_stage="xhs_artifact_evidence_inconsistent",
                    error_category="artifact_evidence_inconsistent",
                    lease_expires_at=None,
                    completed_at=None,
                    updated_at=now,
                )
            )
            job_id = journal.job_id
            session.commit()
        return self._read_committed_job(job_id)

    def _reconcile_artifact_promotions(self) -> None:
        """Resolve only durable journal entries; never scan arbitrary JSON files."""
        with _JOURNAL_RECONCILE_LOCK:
            with self.database.session() as session:
                journal_ids = session.scalars(
                    select(XhsArtifactPromotionJournalRecord.id).where(
                        (XhsArtifactPromotionJournalRecord.state != "completed")
                        | (
                            XhsArtifactPromotionJournalRecord.resolution
                            == "committed"
                        )
                    ).order_by(
                        XhsArtifactPromotionJournalRecord.created_at,
                        XhsArtifactPromotionJournalRecord.id,
                    )
                ).all()
            for journal_id in journal_ids:
                try:
                    self._reconcile_artifact_promotion(journal_id)
                except Exception:
                    self._mark_artifact_inconsistent(journal_id)

    def _reconcile_artifact_promotion(self, journal_id: str) -> None:
        if not self._claim_journal(journal_id):
            return
        with self.database.session() as session:
            journal = session.get(XhsArtifactPromotionJournalRecord, journal_id)
            if journal is None or journal.owner_token != self._journal_owner or (
                journal.state == "completed"
                and journal.resolution == "inconsistent"
            ):
                return
            snapshot = {
                "id": journal.id,
                "job_id": journal.job_id,
                "artifact_kind": journal.artifact_kind,
                "producer": journal.producer,
                "stage_path": journal.stage_path,
                "final_path": journal.final_path,
                "sha256": journal.sha256,
                "size_bytes": journal.size_bytes,
                "file_dev": journal.file_dev,
                "file_ino": journal.file_ino,
                "file_mtime_ns": journal.file_mtime_ns,
                "target_state": journal.target_state,
                "state": journal.state,
                "resolution": journal.resolution,
                "artifact_id": journal.artifact_id,
            }
            job = session.get(JobRecord, journal.job_id)
            artifacts = session.scalars(
                select(JobArtifactRecord).where(
                    JobArtifactRecord.job_id == journal.job_id
                )
            ).all()

        if snapshot["state"] == "allocating" or any(
            snapshot[field] is None
            for field in ("file_dev", "file_ino", "file_mtime_ns")
        ):
            self._mark_artifact_inconsistent(journal_id)
            return

        store = TrustedXhsArtifactStore(
            self.runtime_dir,
            max_bytes=self._max_artifact_bytes,
        )
        staged = _StagedArtifact(
            journal_id=str(snapshot["id"]),
            store=store,
            stage_name=Path(str(snapshot["stage_path"])).name,
            final_name=Path(str(snapshot["final_path"])).name,
            relative_path=Path(str(snapshot["final_path"])),
            digest=str(snapshot["sha256"]),
            size_bytes=int(snapshot["size_bytes"]),
            identity=XhsArtifactIdentity(
                int(snapshot["file_dev"]),
                int(snapshot["file_ino"]),
                int(snapshot["size_bytes"]),
                int(snapshot["file_mtime_ns"]),
            ),
            promoted=snapshot["state"] in {"promoted", "completed"},
        )
        try:
            matching_artifact = None
            if len(artifacts) == 1:
                candidate = artifacts[0]
                metadata = dict(candidate.metadata_json)
                if (
                    candidate.id == snapshot["artifact_id"]
                    or snapshot["artifact_id"] is None
                ) and (
                    candidate.kind == snapshot["artifact_kind"]
                    and candidate.producer == snapshot["producer"]
                    and candidate.path == snapshot["final_path"]
                    and metadata.get("sha256") == snapshot["sha256"]
                    and metadata.get("size_bytes") == snapshot["size_bytes"]
                ):
                    matching_artifact = candidate
            database_committed = bool(
                job is not None
                and JobState(job.state).value == snapshot["target_state"]
                and matching_artifact is not None
            )
            journal_committed = (
                snapshot["state"] == "completed"
                and snapshot["resolution"] == "committed"
            )
            if database_committed or journal_committed:
                if not database_committed or matching_artifact is None:
                    self._mark_artifact_inconsistent(journal_id)
                    return
                final = store.inspect_final(staged.final_name)
                stage = store.inspect_stage(staged.stage_name)
                if final.status == "missing" and stage.status == "trusted":
                    if not self._journal_file_matches(
                        store,
                        "stage",
                        staged,
                    ):
                        self._mark_artifact_inconsistent(journal_id)
                        return
                    store.recover_promote(
                        staged.stage_name,
                        staged.final_name,
                        staged.identity,
                    )
                    final = store.inspect_final(staged.final_name)
                    stage = store.inspect_stage(staged.stage_name)
                if (
                    final.status != "trusted"
                    or final.identity != staged.identity
                    or not self._journal_file_matches(store, "final", staged)
                    or stage.status not in {"missing"}
                ):
                    self._mark_artifact_inconsistent(journal_id)
                    return
                self._confirm_reconciled_commit(
                    journal_id,
                    artifact_id=matching_artifact.id,
                )
                return
            if artifacts:
                self._mark_artifact_inconsistent(journal_id)
                return
            self._resolve_rolled_back(staged)
        finally:
            store.close()

    def _journal_file_matches(
        self,
        store: TrustedXhsArtifactStore,
        area: str,
        staged: _StagedArtifact,
    ) -> bool:
        try:
            encoded = (
                store.read_stage(staged.stage_name, staged.identity)
                if area == "stage"
                else store.read_final(staged.final_name, staged.identity)
            )
            return (
                len(encoded) == staged.size_bytes
                and hashlib.sha256(encoded).hexdigest() == staged.digest
            )
        except (OSError, ValueError, TypeError):
            return False

    def _confirm_reconciled_commit(
        self,
        journal_id: str,
        *,
        artifact_id: int,
    ) -> None:
        now = self.clock()
        with self.database.session() as session:
            journal = session.get(XhsArtifactPromotionJournalRecord, journal_id)
            if journal is None or journal.owner_token != self._journal_owner:
                return
            journal.state = "completed"
            journal.resolution = "committed"
            journal.artifact_id = artifact_id
            journal.owner_token = None
            journal.recovery_lease_expires_at = None
            journal.updated_at = now
            journal.completed_at = journal.completed_at or now
            session.commit()

    def _discard_staging_path(self, path: Path) -> None:
        if not discard_xhs_staging_file(
            self.runtime_dir,
            path,
            max_bytes=self._max_artifact_bytes,
        ):
            raise OSError("XHS staging cleanup was not safely authorized.")

    def _committed_result(self, job_id: str, *, digest: str | None = None) -> bool:
        try:
            job = self.job_service.get(job_id)
            if len(job.artifacts) != 1:
                return False
            artifact = job.artifacts[0]
            with self.database.session() as session:
                journal = session.scalar(
                    select(XhsArtifactPromotionJournalRecord).where(
                        XhsArtifactPromotionJournalRecord.job_id == job_id,
                        XhsArtifactPromotionJournalRecord.state == "completed",
                        XhsArtifactPromotionJournalRecord.resolution == "committed",
                        XhsArtifactPromotionJournalRecord.owner_token.is_(None),
                        XhsArtifactPromotionJournalRecord.recovery_lease_expires_at.is_(None),
                    )
                )
            if journal is None or (digest is not None and journal.sha256 != digest):
                return False
            self._read_committed_artifact(job_id, artifact)
            return True
        except Exception:
            return False

    def _read_committed_artifact(self, job_id: str, artifact: Any) -> bytes:
        with self.database.session() as session:
            journal = session.scalar(
                select(XhsArtifactPromotionJournalRecord).where(
                    XhsArtifactPromotionJournalRecord.job_id == job_id,
                    XhsArtifactPromotionJournalRecord.artifact_id
                    == artifact.metadata.get("artifact_id"),
                    XhsArtifactPromotionJournalRecord.state == "completed",
                    XhsArtifactPromotionJournalRecord.resolution == "committed",
                    XhsArtifactPromotionJournalRecord.owner_token.is_(None),
                    XhsArtifactPromotionJournalRecord.recovery_lease_expires_at.is_(None),
                )
            )
            if journal is None:
                raise UnsafeXhsArtifactStore("XHS artifact has no committed journal.")
            snapshot = (
                journal.final_path,
                journal.sha256,
                journal.size_bytes,
                journal.file_dev,
                journal.file_ino,
                journal.file_mtime_ns,
            )
        final_path, digest, size, file_dev, file_ino, file_mtime_ns = snapshot
        if (
            artifact.path != final_path
            or artifact.metadata.get("sha256") != digest
            or artifact.metadata.get("size_bytes") != size
        ):
            raise UnsafeXhsArtifactStore("XHS artifact metadata changed.")
        identity = XhsArtifactIdentity(file_dev, file_ino, size, file_mtime_ns)
        with TrustedXhsArtifactStore(
            self.runtime_dir,
            max_bytes=self._max_artifact_bytes,
        ) as store:
            encoded = store.read_final(Path(final_path).name, identity)
        if len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest:
            raise UnsafeXhsArtifactStore("XHS artifact evidence changed.")
        return encoded

    def _read_committed_job(self, job_id: str) -> Job:
        deadline = monotonic() + 0.05
        while True:
            try:
                return self.job_service.get(job_id)
            except Exception:
                if monotonic() >= deadline:
                    raise
                sleep(0.002)

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


def _result_bytes(
    job: Job,
    result: CollectionResult,
    collected_at: datetime,
    *,
    max_bytes: int = DEFAULT_XHS_ARTIFACT_MAX_BYTES,
) -> bytes:
    payload = {
        "schema_version": 1, "job_id": job.id, "job_type": job.type,
        "collected_at": collected_at.isoformat(), "result": result.model_dump(mode="json"),
    }
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    encoded = bytearray()
    for text in encoder.iterencode(payload):
        chunk = text.encode("utf-8")
        if len(encoded) + len(chunk) > max_bytes:
            raise CollectionResultTooLarge("XHS result artifact exceeds its hard cap.")
        encoded.extend(chunk)
    return bytes(encoded)


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
    try:
        owner_id = canonical_owner_id(item.data, item.raw_evidence)
    except OwnerIdentityError as error:
        raise ValueError("search note owner aliases conflict") from error
    return SearchNoteRead(
        note_id=note_id, source_url=str(item.source_url),
        title=data.get("title") if isinstance(data.get("title"), str) else None,
        summary=next((data[name] for name in ("summary", "description", "desc") if isinstance(data.get(name), str)), None),
        user_id=owner_id,
    )


def _validate_search_owners(result: CollectionResult) -> None:
    for item in result.items:
        if item.kind == "note":
            canonical_owner_id(item.data, item.raw_evidence)


def _redacted_result(result: CollectionResult) -> CollectionResult:
    return CollectionResult.model_validate(
        redact_credentials(result.model_dump(mode="python"))
    )
