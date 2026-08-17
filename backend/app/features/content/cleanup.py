"""Durable, lease-driven quarantine for abandoned content artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
import stat
from threading import Lock
from typing import Callable, Literal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError

from backend.app.db import Database, canonical_artifact_path_key, is_canonical_uuid_text
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

    def __post_init__(self) -> None:
        path_key = canonical_artifact_path_key(self.relative_path)
        if (
            self.owner_type not in {"material", "content_package"}
            or not is_canonical_uuid_text(self.owner_id)
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
        with self.database.session() as session:
            statement = sqlite_insert(ArtifactCleanupRecord).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=["owner_type", "owner_id", "path_key"],
                index_where=ArtifactCleanupRecord.state.in_(OPEN_STATES),
            )
            session.execute(statement)
            session.commit()
            record = session.scalar(
                select(ArtifactCleanupRecord).where(
                    ArtifactCleanupRecord.owner_type == candidate.owner_type,
                    ArtifactCleanupRecord.owner_id == candidate.owner_id,
                    ArtifactCleanupRecord.path_key == path_key,
                    ArtifactCleanupRecord.state.in_(OPEN_STATES),
                )
            )
            if record is None:
                raise RuntimeError("Cleanup enqueue committed without an open record.")
            if (
                record.relative_path != candidate.relative_path
                or record.expected_sha256 != candidate.expected_sha256
                or record.expected_size_bytes != candidate.expected_size_bytes
            ):
                raise ValueError("Open cleanup identity conflicts with the candidate.")
            return _read(record)

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
            return self._mark_deleted(record.id, token, now, "already_missing")
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
        moved = rename_contained_regular_to_directory(
            self.runtime_dir,
            record.relative_path,
            quarantine_relative,
            expected_identity=second.identity,
        )
        if moved.status != "trusted":
            return self._needs_human(
                record.id, token, f"quarantine_move_{moved.absolute_key or 'failed'}"
            )
        moved = self._verified_file(quarantine_relative, record)
        post_reference_issue = self._reference_issue(record, moved)
        if (
            moved.status != "trusted"
            or moved.identity != second.identity
            or post_reference_issue is not None
            or not self._lease_owned(record.id, token)
        ):
            category = post_reference_issue or "quarantine_move_outcome_ambiguous"
            return self._needs_human(record.id, token, category)
        if moved.identity is None:
            return self._needs_human(record.id, token, "quarantine_identity_missing")
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
                    return self.get_record(record.id) or record
        except SQLAlchemyError:
            return self._needs_human(record.id, token, "quarantine_commit_ambiguous")
        return self.get_record(record.id) or record

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
            return self._mark_deleted(record.id, token, now, "already_missing")
        if inspection.identity is None or not self._lease_owned(record.id, token):
            return self.get_record(record.id) or record
        expected_identity = inspection.identity
        deleted_on_disk = False
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
                        ArtifactCleanupRecord.lease_expires_at >= delete_time,
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
                        deleted_on_disk = True
                        result = connection.execute(
                            update(ArtifactCleanupRecord)
                            .where(
                                ArtifactCleanupRecord.id == record.id,
                                ArtifactCleanupRecord.state == "claimed",
                                ArtifactCleanupRecord.lease_token == token,
                                ArtifactCleanupRecord.lease_expires_at >= delete_time,
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
            except (SQLAlchemyError, RuntimeError):
                try:
                    connection.rollback()
                except SQLAlchemyError:
                    pass
            finally:
                connection.close()

        if deleted_on_disk:
            return self.get_record(record.id) or record
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

        references: list[tuple[str, str]] = []
        for material_id, material_path in materials:
            if record.owner_type == "material" and material_id == record.owner_id:
                return "live_reference" if (
                    canonical_artifact_path_key(material_path) == record.path_key
                ) else "owner_identity_mismatch"
            references.append(("material", material_path))
        for package_id, package_path, package_status, package_sha, package_size in packages:
            package_key = canonical_artifact_path_key(package_path)
            exact_failed_owner = (
                record.owner_type == "content_package"
                and package_id == record.owner_id
                and package_status == "failed"
                and package_key == record.path_key
                and package_sha == record.expected_sha256
                and package_size == record.expected_size_bytes
            )
            if exact_failed_owner:
                continue
            if record.owner_type == "content_package" and package_id == record.owner_id:
                return "owner_identity_mismatch"
            references.append(("package", package_path))
        for _, cleanup_path, cleanup_key, quarantine_path, _ in cleanups:
            if cleanup_key == record.path_key:
                return "other_cleanup_reference"
            references.append(("cleanup", cleanup_path))
            if quarantine_path is not None:
                references.append(("cleanup", quarantine_path))

        for _kind, relative_path in references:
            reference_key = canonical_artifact_path_key(relative_path)
            if reference_key is None:
                return "ambiguous_reference"
            if reference_key == record.path_key:
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
        now: datetime,
        category: str | None,
    ) -> ArtifactCleanupRead:
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
                    state="deleted",
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_category=category,
                    updated_at=now,
                    completed_at=now,
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
