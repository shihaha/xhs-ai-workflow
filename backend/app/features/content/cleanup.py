"""Durable, lease-driven quarantine for abandoned content artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
import stat
from threading import Event, Lock, Thread
from typing import Callable, Literal, Protocol
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from backend.app.db import (
    Database,
    canonical_artifact_path_key,
    is_canonical_uuid_text,
    windows_artifact_reference_path_key,
)
from backend.app.features.content import export as content_export
from backend.app.features.content.export import (
    ArtifactPathInspection,
    MAX_PACKAGE_BYTES,
    inspect_contained_artifact,
    open_contained_delete_handle,
    read_contained_regular,
    rename_contained_regular_to_directory,
    windows_artifact_references_conflict,
)
from backend.app.features.content.models import (
    ArtifactCleanupRecord,
    ContentPackageRecord,
    ProductMaterialRecord,
)
from backend.app.features.content.schemas import ArtifactCleanupRead


CleanupOwnerType = Literal["material", "content_package"]
OPEN_STATES = ("pending", "claimed", "quarantined", "needs_human")
CLAIMABLE_STATES = ("pending", "quarantined")


class _CleanupBatchService(Protocol):
    def run_due_once(self, *, limit: int = 10) -> int: ...


class ArtifactCleanupWorker:
    """Run one bounded durable-cleanup batch per interruptible poll interval."""

    def __init__(
        self,
        service: _CleanupBatchService,
        *,
        poll_seconds: float,
        batch_size: int,
    ) -> None:
        if poll_seconds <= 0 or batch_size < 1:
            raise ValueError("Cleanup worker settings must be positive.")
        self.service = service
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self.last_error_category: str | None = None
        self._stop_event = Event()
        self._lifecycle_lock = Lock()
        self._thread: Thread | None = None

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None:
                return
            self._thread = Thread(
                target=self._run,
                name="artifact-cleanup",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=0.25)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.service.run_due_once(limit=self.batch_size)
            except Exception:
                # The worker must keep polling without retaining sensitive exception text.
                self.last_error_category = "cleanup_worker_error"
            if self._stop_event.wait(self.poll_seconds):
                return


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class ArtifactCleanupCandidate:
    owner_type: CleanupOwnerType
    owner_id: str
    relative_path: str
    expected_sha256: str
    expected_size_bytes: int
    reason: str
    not_before: datetime
    source_build_token: str | None = None

    def __post_init__(self) -> None:
        path_key = canonical_artifact_path_key(self.relative_path)
        if (
            self.owner_type not in {"material", "content_package"}
            or not is_canonical_uuid_text(self.owner_id)
            or (
                self.source_build_token is not None
                if self.owner_type == "material"
                else not is_canonical_uuid_text(self.source_build_token)
            )
            or path_key is None
            or len(self.relative_path) > 1000
            or len(self.expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.expected_sha256)
            or not 0 <= self.expected_size_bytes <= MAX_PACKAGE_BYTES
            or not self.reason.strip()
            or len(self.reason) > 64
        ):
            raise ValueError("Artifact cleanup candidate is not canonical.")
        _naive_utc(self.not_before)


class ArtifactCleanupService:
    """Move unreferenced artifacts to quarantine and delete them after a grace period."""

    def __init__(
        self,
        database: Database,
        *,
        runtime_dir: Path,
        clock: Callable[[], datetime] = _utc_now,
        grace_period: timedelta = timedelta(hours=24),
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        self.database = database
        self.runtime_dir = runtime_dir.resolve(strict=True)
        self.clock = clock
        self.grace_period = grace_period
        self.lease_duration = lease_duration
        self._owned_leases: dict[str, str] = {}
        self._lease_lock = Lock()

    def enqueue(self, candidate: ArtifactCleanupCandidate) -> ArtifactCleanupRead:
        with self.database.session() as session:
            record = self.enqueue_in_session(session, candidate)
            session.commit()
            return record

    def enqueue_in_session(
        self, session: Session, candidate: ArtifactCleanupCandidate
    ) -> ArtifactCleanupRead:
        """Insert or obtain one cleanup fact without committing the caller's transaction."""
        path_key = canonical_artifact_path_key(candidate.relative_path)
        if (
            candidate.owner_type not in {"material", "content_package"}
            or not is_canonical_uuid_text(candidate.owner_id)
            or path_key is None
            or len(candidate.relative_path) > 1000
            or len(candidate.expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in candidate.expected_sha256)
            or not 0 <= candidate.expected_size_bytes <= MAX_PACKAGE_BYTES
            or not candidate.reason.strip()
            or len(candidate.reason) > 64
        ):
            raise ValueError("Artifact cleanup candidate is not canonical.")
        now = _naive_utc(self.clock())
        cleanup_id = str(uuid4())
        values = {
            "id": cleanup_id,
            "owner_type": candidate.owner_type,
            "owner_id": candidate.owner_id,
            "source_build_token": candidate.source_build_token,
            "relative_path": candidate.relative_path,
            "path_key": path_key,
            "expected_sha256": candidate.expected_sha256,
            "expected_size_bytes": candidate.expected_size_bytes,
            "state": "pending",
            "reason": candidate.reason.strip(),
            "not_before": _naive_utc(candidate.not_before),
            "lease_token": None,
            "lease_expires_at": None,
            "quarantine_path": None,
            "quarantine_volume_id": None,
            "quarantine_file_id": None,
            "quarantine_size_bytes": None,
            "quarantine_mtime_ns": None,
            "attempt_count": 0,
            "last_error_category": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        statement = sqlite_insert(ArtifactCleanupRecord).values(**values)
        statement = statement.on_conflict_do_nothing(
            index_elements=["owner_type", "owner_id", "path_key"],
            index_where=ArtifactCleanupRecord.state.in_(OPEN_STATES),
        )
        session.execute(statement)
        record = session.scalar(
            select(ArtifactCleanupRecord).where(
                ArtifactCleanupRecord.owner_type == candidate.owner_type,
                ArtifactCleanupRecord.owner_id == candidate.owner_id,
                ArtifactCleanupRecord.path_key == path_key,
                ArtifactCleanupRecord.state.in_(OPEN_STATES),
            )
        )
        if record is None:
            raise RuntimeError("Cleanup enqueue produced no open record.")
        if (
            record.relative_path != candidate.relative_path
            or record.source_build_token != candidate.source_build_token
            or record.expected_sha256 != candidate.expected_sha256
            or record.expected_size_bytes != candidate.expected_size_bytes
        ):
            raise ValueError("Open cleanup identity conflicts with the candidate.")
        return _read(record)

    def cancel_in_session(
        self,
        session: Session,
        cleanup_id: str,
        *,
        candidate: ArtifactCleanupCandidate,
    ) -> bool:
        """Cancel one exact pending reservation without committing the transaction."""
        path_key = canonical_artifact_path_key(candidate.relative_path)
        if path_key is None or not is_canonical_uuid_text(cleanup_id):
            raise ValueError("Cleanup cancellation identity is not canonical.")
        now = _naive_utc(self.clock())
        exact = (
            ArtifactCleanupRecord.id == cleanup_id,
            ArtifactCleanupRecord.owner_type == candidate.owner_type,
            ArtifactCleanupRecord.owner_id == candidate.owner_id,
            ArtifactCleanupRecord.relative_path == candidate.relative_path,
            ArtifactCleanupRecord.path_key == path_key,
            ArtifactCleanupRecord.expected_sha256 == candidate.expected_sha256,
            ArtifactCleanupRecord.expected_size_bytes == candidate.expected_size_bytes,
        )
        won = session.execute(
            update(ArtifactCleanupRecord)
            .where(*exact, ArtifactCleanupRecord.state == "pending")
            .values(
                state="cancelled",
                completed_at=now,
                updated_at=now,
                lease_token=None,
                lease_expires_at=None,
            )
        ).rowcount
        if won == 1:
            return True
        return session.scalar(
            select(ArtifactCleanupRecord.id).where(
                *exact, ArtifactCleanupRecord.state == "cancelled"
            )
        ) is not None

    def make_due_in_session(
        self,
        session: Session,
        cleanup_id: str,
        *,
        candidate: ArtifactCleanupCandidate,
        due_at: datetime | None = None,
        reason: str | None = None,
    ) -> bool:
        """Advance one exact pending reservation without committing the transaction."""
        path_key = canonical_artifact_path_key(candidate.relative_path)
        if path_key is None or not is_canonical_uuid_text(cleanup_id):
            raise ValueError("Cleanup due identity is not canonical.")
        if reason is not None and (not reason or len(reason) > 64):
            raise ValueError("Cleanup due reason is invalid.")
        now = _naive_utc(due_at or self.clock())
        exact = (
            ArtifactCleanupRecord.id == cleanup_id,
            ArtifactCleanupRecord.owner_type == candidate.owner_type,
            ArtifactCleanupRecord.owner_id == candidate.owner_id,
            ArtifactCleanupRecord.relative_path == candidate.relative_path,
            ArtifactCleanupRecord.path_key == path_key,
            ArtifactCleanupRecord.expected_sha256 == candidate.expected_sha256,
            ArtifactCleanupRecord.expected_size_bytes == candidate.expected_size_bytes,
            ArtifactCleanupRecord.state == "pending",
        )
        values: dict[str, object] = {"not_before": now, "updated_at": now}
        if reason is not None:
            values["reason"] = reason
        won = session.execute(
            update(ArtifactCleanupRecord).where(*exact).values(**values)
        ).rowcount
        return won == 1

    def claim_due(self, *, limit: int = 10) -> list[str]:
        if limit < 1:
            return []
        now = _naive_utc(self.clock())
        with self.database.session() as session:
            due_ids = list(
                session.scalars(
                    select(ArtifactCleanupRecord.id)
                    .where(
                        ArtifactCleanupRecord.state.in_(CLAIMABLE_STATES),
                        ArtifactCleanupRecord.not_before <= now,
                    )
                    .order_by(
                        ArtifactCleanupRecord.created_at,
                        ArtifactCleanupRecord.id,
                    )
                    .limit(limit)
                )
            )
        claimed: list[str] = []
        for cleanup_id in due_ids:
            token = self._claim_specific(cleanup_id, now=now)
            if token is not None:
                claimed.append(cleanup_id)
        return claimed

    def process_one(self, cleanup_id: str) -> ArtifactCleanupRead:
        record = self.get_record(cleanup_id)
        if record is None:
            raise KeyError(cleanup_id)
        now = _naive_utc(self.clock())
        with self._lease_lock:
            token = self._owned_leases.get(cleanup_id)
        if token is None and record.state in CLAIMABLE_STATES and record.not_before <= now:
            token = self._claim_specific(cleanup_id, now=now)
        if token is None:
            return self.get_record(cleanup_id) or record
        try:
            claimed = self._load_claimed(cleanup_id, token)
            if claimed is None:
                return self.get_record(cleanup_id) or record
            try:
                if claimed.quarantine_path is None:
                    return self._quarantine(claimed, token, now)
                return self._delete_quarantined(claimed, token, now)
            except (OSError, OverflowError, ValidationError, ValueError):
                return self._needs_human(cleanup_id, token, "cleanup_processing_error")
        finally:
            with self._lease_lock:
                self._owned_leases.pop(cleanup_id, None)

    def recover_expired_leases(self) -> int:
        now = _naive_utc(self.clock())
        with self.database.session() as session:
            expired = list(
                session.execute(
                    select(
                        ArtifactCleanupRecord.id,
                        ArtifactCleanupRecord.lease_token,
                        ArtifactCleanupRecord.lease_expires_at,
                        ArtifactCleanupRecord.quarantine_path,
                    ).where(
                        ArtifactCleanupRecord.state == "claimed",
                        ArtifactCleanupRecord.lease_expires_at < now,
                    )
                )
            )
            recovered_ids: list[str] = []
            for record in expired:
                result = session.execute(
                    update(ArtifactCleanupRecord)
                    .where(
                        ArtifactCleanupRecord.id == record.id,
                        ArtifactCleanupRecord.state == "claimed",
                        ArtifactCleanupRecord.lease_token == record.lease_token,
                        ArtifactCleanupRecord.lease_expires_at == record.lease_expires_at,
                        ArtifactCleanupRecord.lease_expires_at < now,
                    )
                    .values(
                        state="quarantined" if record.quarantine_path else "pending",
                        lease_token=None,
                        lease_expires_at=None,
                        attempt_count=ArtifactCleanupRecord.attempt_count + 1,
                        last_error_category="lease_expired",
                        updated_at=now,
                    )
                )
                if result.rowcount == 1:
                    recovered_ids.append(record.id)
            session.commit()
        if recovered_ids:
            with self._lease_lock:
                for cleanup_id in recovered_ids:
                    self._owned_leases.pop(cleanup_id, None)
        return len(recovered_ids)

    def run_due_once(self, *, limit: int = 10) -> int:
        cleanup_ids = self.claim_due(limit=limit)
        for cleanup_id in cleanup_ids:
            self.process_one(cleanup_id)
        return len(cleanup_ids)

    def list_records(self) -> list[ArtifactCleanupRead]:
        with self.database.session() as session:
            records = list(
                session.scalars(
                    select(ArtifactCleanupRecord).order_by(
                        ArtifactCleanupRecord.created_at,
                        ArtifactCleanupRecord.id,
                    )
                )
            )
            return [_read(record) for record in records]

    def get_record(self, cleanup_id: str) -> ArtifactCleanupRead | None:
        with self.database.session() as session:
            record = session.get(ArtifactCleanupRecord, cleanup_id)
            return _read(record) if record is not None else None

    def _claim_specific(self, cleanup_id: str, *, now: datetime) -> str | None:
        token = str(uuid4())
        with self.database.session() as session:
            result = session.execute(
                update(ArtifactCleanupRecord)
                .where(
                    ArtifactCleanupRecord.id == cleanup_id,
                    ArtifactCleanupRecord.state.in_(CLAIMABLE_STATES),
                    ArtifactCleanupRecord.not_before <= now,
                )
                .values(
                    state="claimed",
                    lease_token=token,
                    lease_expires_at=now + self.lease_duration,
                    updated_at=now,
                )
            )
            session.commit()
            if result.rowcount != 1:
                return None
        with self._lease_lock:
            self._owned_leases[cleanup_id] = token
        return token

    def _load_claimed(self, cleanup_id: str, token: str) -> ArtifactCleanupRead | None:
        with self.database.session() as session:
            record = session.scalar(
                select(ArtifactCleanupRecord).where(
                    ArtifactCleanupRecord.id == cleanup_id,
                    ArtifactCleanupRecord.state == "claimed",
                    ArtifactCleanupRecord.lease_token == token,
                )
            )
            return _read(record) if record is not None else None

    def _lease_owned(self, cleanup_id: str, token: str) -> bool:
        with self.database.session() as session:
            return session.scalar(
                select(ArtifactCleanupRecord.id).where(
                    ArtifactCleanupRecord.id == cleanup_id,
                    ArtifactCleanupRecord.state == "claimed",
                    ArtifactCleanupRecord.lease_token == token,
                    ArtifactCleanupRecord.lease_expires_at >= _naive_utc(self.clock()),
                )
            ) is not None

    def _quarantine(
        self, record: ArtifactCleanupRead, token: str, now: datetime
    ) -> ArtifactCleanupRead:
        raw_inspection = inspect_contained_artifact(self.runtime_dir, record.relative_path)
        inspection = self._verified_file(record.relative_path, record)
        quarantine_relative = (
            PurePosixPath("artifacts-quarantine")
            / record.id
            / PurePosixPath(record.relative_path).name
        ).as_posix()
        quarantine_existing = inspect_contained_artifact(
            self.runtime_dir, quarantine_relative
        )
        if quarantine_existing.status != "missing":
            if (
                quarantine_existing.status == "trusted"
                and inspection.status == "missing"
                and self._lease_owned(record.id, token)
            ):
                recovered = self._verified_file(quarantine_relative, record)
                if recovered.status == "trusted" and recovered.identity is not None:
                    reference_issue = self._reference_issue(record, recovered)
                    return self._mark_moved_needs_human(
                        record.id,
                        token,
                        reference_issue or "quarantine_move_recovered",
                        quarantine_relative,
                        recovered.identity,
                    )
            return self._needs_human(record.id, token, "quarantine_target_ambiguous")
        if inspection.status == "ambiguous":
            category = (
                "identity_mismatch"
                if raw_inspection.status == "trusted"
                else "ambiguous_path"
            )
            return self._needs_human(record.id, token, category)
        if not self._lease_owned(record.id, token):
            return self.get_record(record.id) or record
        reference_issue = self._reference_issue(record, inspection)
        if reference_issue is not None:
            return self._needs_human(record.id, token, reference_issue)
        if inspection.status == "missing":
            return self._mark_deleted(record.id, token, "already_missing")
        if not self._lease_owned(record.id, token):
            return self.get_record(record.id) or record

        target = self._prepare_quarantine_target(record.id, quarantine_relative)
        if target is None or inspection.path is None or inspection.identity is None:
            return self._needs_human(record.id, token, "ambiguous_quarantine_path")
        second = self._verified_file(record.relative_path, record)
        if (
            second.status != "trusted"
            or second.identity != inspection.identity
            or second.path is None
            or not self._lease_owned(record.id, token)
        ):
            return self._needs_human(record.id, token, "identity_changed_before_move")
        rename_result = rename_contained_regular_to_directory(
            self.runtime_dir,
            record.relative_path,
            quarantine_relative,
            expected_identity=second.identity,
        )
        if rename_result.status != "trusted":
            return self._needs_human(
                record.id,
                token,
                f"quarantine_move_{rename_result.absolute_key or 'failed'}",
            )
        moved_identity = rename_result.identity or second.identity
        moved = self._verified_file(quarantine_relative, record)
        post_reference_issue = self._reference_issue(record, moved)
        if (
            moved.status != "trusted"
            or moved.identity != moved_identity
            or post_reference_issue is not None
            or not self._lease_owned(record.id, token)
        ):
            category = post_reference_issue or "quarantine_move_outcome_ambiguous"
            return self._mark_moved_needs_human(
                record.id,
                token,
                category,
                quarantine_relative,
                moved_identity,
            )
        if moved.identity is None:
            return self._mark_moved_needs_human(
                record.id,
                token,
                "quarantine_identity_missing",
                quarantine_relative,
                moved_identity,
            )
        quarantined_at = _naive_utc(self.clock())
        try:
            with self.database.session() as session:
                result = session.execute(
                    update(ArtifactCleanupRecord)
                    .where(
                        ArtifactCleanupRecord.id == record.id,
                        ArtifactCleanupRecord.state == "claimed",
                        ArtifactCleanupRecord.lease_token == token,
                        ArtifactCleanupRecord.lease_expires_at >= quarantined_at,
                    )
                    .values(
                        state="quarantined",
                        quarantine_path=quarantine_relative,
                        quarantine_volume_id=moved.identity[0],
                        quarantine_file_id=moved.identity[1],
                        quarantine_size_bytes=moved.identity[2],
                        quarantine_mtime_ns=moved.identity[3],
                        not_before=quarantined_at + self.grace_period,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error_category=None,
                        updated_at=quarantined_at,
                    )
                )
                session.commit()
                if result.rowcount != 1:
                    latest = self.get_record(record.id)
                    if self._moved_fact_matches(
                        latest,
                        quarantine_relative,
                        moved_identity,
                        states={"quarantined"},
                    ):
                        return latest  # type: ignore[return-value]
                    return self._mark_moved_needs_human(
                        record.id,
                        token,
                        "quarantine_commit_ambiguous",
                        quarantine_relative,
                        moved_identity,
                    )
        except SQLAlchemyError:
            latest = self.get_record(record.id)
            if self._moved_fact_matches(
                latest,
                quarantine_relative,
                moved_identity,
                states={"quarantined"},
            ):
                return latest  # type: ignore[return-value]
            return self._mark_moved_needs_human(
                record.id,
                token,
                "quarantine_commit_ambiguous",
                quarantine_relative,
                moved_identity,
            )
        return self.get_record(record.id) or record

    def _mark_moved_needs_human(
        self,
        cleanup_id: str,
        token: str,
        category: str,
        quarantine_path: str,
        identity: tuple[int, int, int, int],
    ) -> ArtifactCleanupRead:
        fresh_now = _naive_utc(self.clock())
        try:
            with self.database.session() as session:
                result = session.execute(
                    update(ArtifactCleanupRecord)
                    .where(
                        ArtifactCleanupRecord.id == cleanup_id,
                        ArtifactCleanupRecord.state == "claimed",
                        ArtifactCleanupRecord.lease_token == token,
                        ArtifactCleanupRecord.lease_expires_at > fresh_now,
                    )
                    .values(
                        state="needs_human",
                        quarantine_path=quarantine_path,
                        quarantine_volume_id=identity[0],
                        quarantine_file_id=identity[1],
                        quarantine_size_bytes=identity[2],
                        quarantine_mtime_ns=identity[3],
                        lease_token=None,
                        lease_expires_at=None,
                        last_error_category=category[:64],
                        updated_at=fresh_now,
                    )
                )
                session.commit()
                if result.rowcount != 1:
                    latest = self.get_record(cleanup_id)
                    if latest is None:
                        raise RuntimeError("Cleanup record disappeared.")
                    return latest
        except SQLAlchemyError:
            pass
        latest = self.get_record(cleanup_id)
        if latest is None:
            raise RuntimeError("Cleanup record disappeared.")
        if self._moved_fact_matches(
            latest, quarantine_path, identity, states={"needs_human"}
        ) and latest.last_error_category == category[:64]:
            return latest
        return latest

    @staticmethod
    def _moved_fact_matches(
        record: ArtifactCleanupRead | None,
        quarantine_path: str,
        identity: tuple[int, int, int, int],
        *,
        states: set[str],
    ) -> bool:
        return record is not None and (
            record.state in states
            and record.quarantine_path == quarantine_path
            and (
                record.quarantine_volume_id,
                record.quarantine_file_id,
                record.quarantine_size_bytes,
                record.quarantine_mtime_ns,
            )
            == identity
        )

    def _delete_quarantined(
        self, record: ArtifactCleanupRead, token: str, now: datetime
    ) -> ArtifactCleanupRead:
        if record.quarantine_path is None:
            return self._needs_human(record.id, token, "missing_quarantine_identity")
        if not self._lease_owned(record.id, token):
            return self.get_record(record.id) or record
        original = inspect_contained_artifact(self.runtime_dir, record.relative_path)
        if original.status != "missing":
            return self._needs_human(record.id, token, "original_path_reappeared")
        raw_inspection = inspect_contained_artifact(
            self.runtime_dir, record.quarantine_path
        )
        inspection = self._verified_file(record.quarantine_path, record)
        if inspection.status == "ambiguous":
            category = (
                "identity_mismatch"
                if raw_inspection.status == "trusted"
                else "ambiguous_quarantine_path"
            )
            return self._needs_human(record.id, token, category)
        persisted_identity = (
            record.quarantine_volume_id,
            record.quarantine_file_id,
            record.quarantine_size_bytes,
            record.quarantine_mtime_ns,
        )
        if inspection.status == "trusted" and inspection.identity != persisted_identity:
            return self._needs_human(
                record.id, token, "quarantine_identity_changed"
            )
        reference_snapshot = self._reference_snapshot(record.id)
        if reference_snapshot is None:
            return self._needs_human(record.id, token, "reference_check_failed")
        reference_issue = self._reference_issue_from_snapshot(
            record, inspection, reference_snapshot
        )
        if reference_issue is not None:
            return self._needs_human(record.id, token, reference_issue)
        if inspection.status == "missing":
            return self._mark_deleted(record.id, token, "already_missing")
        if inspection.identity is None or not self._lease_owned(record.id, token):
            return self.get_record(record.id) or record
        expected_identity = inspection.identity
        delete_committed = False
        safely_disarmed = False
        ambiguous_persisted = False
        authorization_issue: str | None = None
        with open_contained_delete_handle(
            self.runtime_dir,
            record.quarantine_path,
            limit=max(record.expected_size_bytes, 1),
            expected_identity=expected_identity,
        ) as descriptor:
            if descriptor is None:
                return self._needs_human(record.id, token, "delete_handle_failed")
            if inspect_contained_artifact(
                self.runtime_dir, record.relative_path
            ).status != "missing":
                return self._needs_human(
                    record.id, token, "original_path_reappeared"
                )
            connection = self.database.engine.connect()
            armed = False
            delete_time: datetime | None = None

            def recover_armed_failure() -> None:
                nonlocal armed, safely_disarmed, ambiguous_persisted
                try:
                    disarmed = content_export._set_delete_disposition(
                        descriptor, False
                    )
                except (OSError, OverflowError, ValueError, TypeError):
                    disarmed = False
                if disarmed:
                    armed = False
                    safely_disarmed = True
                    if connection.in_transaction():
                        connection.rollback()
                        return
                ambiguity_time = _naive_utc(self.clock())
                if not connection.in_transaction():
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                result = connection.execute(
                    update(ArtifactCleanupRecord)
                    .where(
                        ArtifactCleanupRecord.id == record.id,
                        ArtifactCleanupRecord.quarantine_path
                        == record.quarantine_path,
                        ArtifactCleanupRecord.quarantine_volume_id
                        == expected_identity[0],
                        ArtifactCleanupRecord.quarantine_file_id
                        == expected_identity[1],
                        ArtifactCleanupRecord.quarantine_size_bytes
                        == expected_identity[2],
                        ArtifactCleanupRecord.quarantine_mtime_ns
                        == expected_identity[3],
                        or_(
                            and_(
                                ArtifactCleanupRecord.state == "claimed",
                                ArtifactCleanupRecord.lease_token == token,
                                ArtifactCleanupRecord.lease_expires_at
                                > ambiguity_time,
                            ),
                            and_(
                                ArtifactCleanupRecord.state == "deleted",
                                ArtifactCleanupRecord.completed_at == delete_time,
                            ),
                        ),
                    )
                    .values(
                        state="needs_human",
                        lease_token=None,
                        lease_expires_at=None,
                        last_error_category="delete_outcome_ambiguous",
                        updated_at=ambiguity_time,
                        completed_at=None,
                    )
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        "Ambiguous handle deletion could not be persisted."
                    )
                connection.commit()
                ambiguous_persisted = True

            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                delete_time = _naive_utc(self.clock())
                current = connection.execute(
                    select(
                        ArtifactCleanupRecord.id,
                        ArtifactCleanupRecord.quarantine_path,
                        ArtifactCleanupRecord.quarantine_volume_id,
                        ArtifactCleanupRecord.quarantine_file_id,
                        ArtifactCleanupRecord.quarantine_size_bytes,
                        ArtifactCleanupRecord.quarantine_mtime_ns,
                    ).where(
                        ArtifactCleanupRecord.id == record.id,
                        ArtifactCleanupRecord.state == "claimed",
                        ArtifactCleanupRecord.lease_token == token,
                        ArtifactCleanupRecord.lease_expires_at > delete_time,
                    )
                ).first()
                if current is None or (
                    current.quarantine_path,
                    current.quarantine_volume_id,
                    current.quarantine_file_id,
                    current.quarantine_size_bytes,
                    current.quarantine_mtime_ns,
                ) != (record.quarantine_path, *expected_identity):
                    authorization_issue = "lease_or_identity_changed"
                    connection.rollback()
                else:
                    locked_snapshot = self._reference_snapshot(
                        record.id, executor=connection
                    )
                    if locked_snapshot is None or locked_snapshot != reference_snapshot:
                        authorization_issue = "reference_set_changed"
                        connection.rollback()
                    elif not content_export._delete_open_file(descriptor):
                        authorization_issue = "delete_failed"
                        connection.rollback()
                    else:
                        armed = True
                        try:
                            result = connection.execute(
                                update(ArtifactCleanupRecord)
                                .where(
                                    ArtifactCleanupRecord.id == record.id,
                                    ArtifactCleanupRecord.state == "claimed",
                                    ArtifactCleanupRecord.lease_token == token,
                                    ArtifactCleanupRecord.lease_expires_at > delete_time,
                                )
                                .values(
                                    state="deleted",
                                    lease_token=None,
                                    lease_expires_at=None,
                                    last_error_category=None,
                                    updated_at=delete_time,
                                    completed_at=delete_time,
                                )
                            )
                            if result.rowcount != 1:
                                raise RuntimeError("Final cleanup lease CAS failed.")
                            connection.commit()
                            delete_committed = True
                        except (SQLAlchemyError, RuntimeError):
                            recover_armed_failure()
            except (SQLAlchemyError, RuntimeError):
                if armed and not ambiguous_persisted:
                    recover_armed_failure()
                else:
                    try:
                        connection.rollback()
                    except SQLAlchemyError:
                        pass
            finally:
                connection.close()

        if delete_committed or ambiguous_persisted:
            return self.get_record(record.id) or record
        if safely_disarmed:
            retained = inspect_contained_artifact(
                self.runtime_dir, record.quarantine_path
            )
            if retained.status == "trusted" and retained.identity == expected_identity:
                return self.get_record(record.id) or record
            return self._needs_human(
                record.id, token, "delete_outcome_ambiguous"
            )
        if authorization_issue == "reference_set_changed":
            refreshed_snapshot = self._reference_snapshot(record.id)
            if refreshed_snapshot is not None:
                authorization_issue = self._reference_issue_from_snapshot(
                    record, inspection, refreshed_snapshot
                ) or authorization_issue
        return self._needs_human(record.id, token, authorization_issue or "delete_failed")

    def _verified_file(
        self, relative_path: str, record: ArtifactCleanupRead
    ) -> ArtifactPathInspection:
        first = inspect_contained_artifact(self.runtime_dir, relative_path)
        if first.status != "trusted":
            return first
        try:
            payload = read_contained_regular(
                self.runtime_dir,
                relative_path,
                limit=max(record.expected_size_bytes, 1),
            )
        except ValueError:
            return ArtifactPathInspection("ambiguous")
        second = inspect_contained_artifact(self.runtime_dir, relative_path)
        if (
            second.status != "trusted"
            or first.identity != second.identity
            or len(payload) != record.expected_size_bytes
            or sha256(payload).hexdigest() != record.expected_sha256
        ):
            return ArtifactPathInspection(
                "ambiguous",
                absolute_key=second.absolute_key,
                path=second.path,
                identity=second.identity,
                size_bytes=second.size_bytes,
            )
        return second

    def _reference_issue(
        self, record: ArtifactCleanupRead, artifact: ArtifactPathInspection
    ) -> str | None:
        snapshot = self._reference_snapshot(record.id)
        if snapshot is None:
            return "reference_check_failed"
        return self._reference_issue_from_snapshot(record, artifact, snapshot)

    def _reference_snapshot(self, cleanup_id: str, *, executor=None):
        try:
            if executor is None:
                with self.database.engine.connect() as connection:
                    return self._reference_snapshot(cleanup_id, executor=connection)
            materials = tuple(
                tuple(row)
                for row in executor.execute(
                    select(
                        ProductMaterialRecord.id,
                        ProductMaterialRecord.path,
                        ProductMaterialRecord.sha256,
                        ProductMaterialRecord.size_bytes,
                    )
                    .order_by(ProductMaterialRecord.id)
                )
            )
            packages = tuple(
                tuple(row)
                for row in executor.execute(
                    select(
                        ContentPackageRecord.id,
                        ContentPackageRecord.path,
                        ContentPackageRecord.status,
                        ContentPackageRecord.sha256,
                        ContentPackageRecord.size_bytes,
                        ContentPackageRecord.build_token,
                    )
                    .order_by(ContentPackageRecord.id)
                )
            )
            cleanups = tuple(
                tuple(row)
                for row in executor.execute(
                    select(
                        ArtifactCleanupRecord.id,
                        ArtifactCleanupRecord.relative_path,
                        ArtifactCleanupRecord.path_key,
                        ArtifactCleanupRecord.quarantine_path,
                        ArtifactCleanupRecord.state,
                    )
                    .where(
                        ArtifactCleanupRecord.id != cleanup_id,
                        ArtifactCleanupRecord.state.in_(OPEN_STATES),
                    )
                    .order_by(ArtifactCleanupRecord.id)
                )
            )
            return materials, packages, cleanups
        except SQLAlchemyError:
            return None

    def _reference_issue_from_snapshot(
        self,
        record: ArtifactCleanupRead,
        artifact: ArtifactPathInspection,
        snapshot,
    ) -> str | None:
        materials, packages, cleanups = snapshot

        quarantine_key = windows_artifact_reference_path_key(
            (
                PurePosixPath("artifacts-quarantine")
                / record.id
                / PurePosixPath(record.relative_path).name
            ).as_posix()
        )
        quarantine_absolute = self.runtime_dir.joinpath(
            *PurePosixPath(
                f"artifacts-quarantine/{record.id}/"
                f"{PurePosixPath(record.relative_path).name}"
            ).parts
        )
        artifact_is_quarantined = (
            artifact.path is not None and artifact.path == quarantine_absolute
        )

        original_windows_key = windows_artifact_reference_path_key(record.relative_path)
        references: list[tuple[str, str, str | None, int | None]] = []
        for material_id, material_path, material_sha, material_size in materials:
            if record.owner_type == "material" and material_id == record.owner_id:
                material_windows_key = windows_artifact_reference_path_key(material_path)
                return "live_reference" if (
                    material_windows_key is not None
                    and material_windows_key == original_windows_key
                    and material_sha == record.expected_sha256
                    and material_size == record.expected_size_bytes
                ) else "owner_identity_mismatch"
            references.append(("material", material_path, material_sha, material_size))
        for (
            package_id, package_path, package_status, package_sha, package_size,
            package_build_token,
        ) in packages:
            package_windows_key = windows_artifact_reference_path_key(package_path)
            exact_current_generation = (
                record.owner_type == "content_package"
                and package_id == record.owner_id
                and package_build_token == record.source_build_token
                and package_windows_key is not None
                and package_windows_key == original_windows_key
                and package_sha == record.expected_sha256
                and package_size == record.expected_size_bytes
            )
            if exact_current_generation:
                if package_status == "failed":
                    continue
                return "live_reference"
            if record.owner_type == "content_package" and package_id == record.owner_id:
                if (
                    package_build_token != record.source_build_token
                    and package_windows_key != original_windows_key
                ):
                    references.append(("package", package_path, package_sha, package_size))
                    continue
                if (
                    quarantine_key is None
                    or package_windows_key != quarantine_key
                    or package_sha != record.expected_sha256
                    or package_size != record.expected_size_bytes
                ):
                    return "owner_identity_mismatch"
            references.append(("package", package_path, package_sha, package_size))
        for _, cleanup_path, cleanup_key, quarantine_path, _ in cleanups:
            if cleanup_key == record.path_key:
                return "other_cleanup_reference"
            references.append(("cleanup", cleanup_path, None, None))
            if quarantine_path is not None:
                references.append(("cleanup", quarantine_path, None, None))

        for kind, relative_path, reference_sha, reference_size in references:
            reference_key = canonical_artifact_path_key(relative_path)
            windows_reference_key = windows_artifact_reference_path_key(relative_path)
            if quarantine_key is not None and windows_reference_key == quarantine_key:
                if (
                    kind != "cleanup"
                    and (
                        reference_sha != record.expected_sha256
                        or reference_size != record.expected_size_bytes
                    )
                ):
                    return "ambiguous_reference"
                if artifact_is_quarantined:
                    return "live_reference"
                # A reference may reserve the deterministic destination while this
                # cleanup is still pending.  Preserve that fact after the move;
                # treating the not-yet-existing destination as an ambiguous source
                # reference would strand the original bytes instead.
                continue
            if reference_key is None:
                return "ambiguous_reference"
            if windows_reference_key is not None and windows_reference_key == original_windows_key:
                if (
                    kind != "cleanup"
                    and (
                        reference_sha != record.expected_sha256
                        or reference_size != record.expected_size_bytes
                    )
                ):
                    return "ambiguous_reference"
                return "live_reference"
            inspected = inspect_contained_artifact(self.runtime_dir, relative_path)
            if inspected.status == "ambiguous":
                return "ambiguous_reference"
            if (
                artifact.status == "trusted"
                and inspected.status == "trusted"
                and windows_artifact_references_conflict(
                    (artifact.absolute_key or "", artifact.identity[:2] if artifact.identity else None),
                    (inspected.absolute_key or "", inspected.identity[:2] if inspected.identity else None),
                )
            ):
                return "live_reference"
        return None

    def _prepare_quarantine_target(
        self, cleanup_id: str, relative_path: str
    ) -> Path | None:
        target = self.runtime_dir.joinpath(*PurePosixPath(relative_path).parts)
        try:
            current = self.runtime_dir
            for part in PurePosixPath(relative_path).parts[:-1]:
                current = current / part
                if current.exists():
                    if current.is_symlink() or (
                        hasattr(current, "is_junction") and current.is_junction()
                    ):
                        return None
                else:
                    current.mkdir()
                current.resolve(strict=True).relative_to(self.runtime_dir)
                if not stat.S_ISDIR(current.stat().st_mode):
                    return None
            if target.exists() or target.is_symlink():
                return None
            target.resolve(strict=False).relative_to(self.runtime_dir)
            if PurePosixPath(relative_path).parts[1] != cleanup_id:
                return None
            return target
        except (OSError, ValueError, IndexError):
            return None

    def _needs_human(
        self, cleanup_id: str, token: str, category: str
    ) -> ArtifactCleanupRead:
        now = _naive_utc(self.clock())
        try:
            with self.database.session() as session:
                session.execute(
                    update(ArtifactCleanupRecord)
                    .where(
                        ArtifactCleanupRecord.id == cleanup_id,
                        ArtifactCleanupRecord.state == "claimed",
                        ArtifactCleanupRecord.lease_token == token,
                        ArtifactCleanupRecord.lease_expires_at >= now,
                    )
                    .values(
                        state="needs_human",
                        lease_token=None,
                        lease_expires_at=None,
                        last_error_category=category[:64],
                        updated_at=now,
                    )
                )
                session.commit()
        except SQLAlchemyError:
            pass
        record = self.get_record(cleanup_id)
        if record is None:
            raise RuntimeError("Cleanup record disappeared.")
        return record

    def _mark_deleted(
        self,
        cleanup_id: str,
        token: str,
        category: str | None,
    ) -> ArtifactCleanupRead:
        fresh_now = _naive_utc(self.clock())
        with self.database.session() as session:
            session.execute(
                update(ArtifactCleanupRecord)
                .where(
                    ArtifactCleanupRecord.id == cleanup_id,
                    ArtifactCleanupRecord.state == "claimed",
                    ArtifactCleanupRecord.lease_token == token,
                    ArtifactCleanupRecord.lease_expires_at > fresh_now,
                )
                .values(
                    state="deleted",
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_category=category,
                    updated_at=fresh_now,
                    completed_at=fresh_now,
                )
            )
            session.commit()
        record = self.get_record(cleanup_id)
        if record is None:
            raise RuntimeError("Cleanup record disappeared.")
        return record


def _read(record: ArtifactCleanupRecord) -> ArtifactCleanupRead:
    return ArtifactCleanupRead.model_validate(
        {
            "id": record.id,
            "owner_type": record.owner_type,
            "owner_id": record.owner_id,
            "source_build_token": record.source_build_token,
            "relative_path": record.relative_path,
            "path_key": record.path_key,
            "expected_sha256": record.expected_sha256,
            "expected_size_bytes": record.expected_size_bytes,
            "state": record.state,
            "reason": record.reason,
            "not_before": record.not_before,
            "lease_token": record.lease_token,
            "lease_expires_at": record.lease_expires_at,
            "quarantine_path": record.quarantine_path,
            "quarantine_volume_id": record.quarantine_volume_id,
            "quarantine_file_id": record.quarantine_file_id,
            "quarantine_size_bytes": record.quarantine_size_bytes,
            "quarantine_mtime_ns": record.quarantine_mtime_ns,
            "attempt_count": record.attempt_count,
            "last_error_category": record.last_error_category,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "completed_at": record.completed_at,
        }
    )
