"""Transactional ranking ingestion, deterministic deduplication and account scoring."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from backend.app.db import Database
from backend.app.features.radar.models import (
    AccountRead,
    RankItemInput,
    RankItemRead,
    RankItemRecord,
    RankSnapshotInput,
    RankSnapshotRead,
    RankSnapshotRecord,
    QianfanCollectionRecord,
)
from backend.app.features.radar.scoring import has_recognized_evidence, score_account
from backend.app.models.jobs import JobRecord, JobState


class RadarJobFinalizationConflict(RuntimeError):
    """Raised when cancellation or another owner won before snapshot commit."""


class RadarService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ingest_snapshot(self, snapshot: RankSnapshotInput) -> RankSnapshotRead:
        chosen = _deduplicate(snapshot.items)
        with self.database.session() as session:
            record = _upsert_snapshot(session, snapshot, chosen)
            session.commit()
            session.refresh(record)
            return _snapshot_read(record)

    def start_qianfan_collection(
        self,
        *,
        collection_id: str,
        source_date: date,
        started_at: datetime,
        expected_count_per_scope: int,
        selector_profile_version: str,
    ) -> None:
        """Persist the execution-time batch identity used to filter same-day reruns."""
        with self.database.session() as session:
            session.add(
                QianfanCollectionRecord(
                    collection_id=collection_id,
                    source_date=source_date.isoformat(),
                    started_at=started_at.isoformat(),
                    expected_count_per_scope=expected_count_per_scope,
                    selector_profile_version=selector_profile_version,
                )
            )
            session.commit()

    def ingest_snapshot_and_finalize_job(
        self,
        snapshot: RankSnapshotInput,
        *,
        job_id: str,
        progress_current: int,
        progress_total: int,
    ) -> RankSnapshotRead:
        """Commit one exact snapshot and its running-job success as one SQLite fact."""
        chosen = _deduplicate(snapshot.items)
        with self.database.session() as session:
            record = _upsert_snapshot(session, snapshot, chosen)
            now = datetime.now(UTC).replace(tzinfo=None)
            result = session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.state == JobState.running.value,
                )
                .values(
                    state=JobState.succeeded.value,
                    progress_current=progress_current,
                    progress_total=progress_total,
                    current_stage="qianfan_scope_complete",
                    error_category=None,
                    updated_at=now,
                    completed_at=now,
                    lease_expires_at=None,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                raise RadarJobFinalizationConflict(
                    "Qianfan scope job no longer owns snapshot finalization."
                )
            session.commit()
            session.refresh(record)
            return _snapshot_read(record)

    def list_snapshots(
        self,
        *,
        source_date: str | date | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RankSnapshotRead]:
        statement = select(RankSnapshotRecord).options(selectinload(RankSnapshotRecord.items))
        if source_date is not None:
            value = source_date.isoformat() if isinstance(source_date, date) else source_date
            statement = statement.where(RankSnapshotRecord.source_date == value)
        statement = statement.order_by(
            RankSnapshotRecord.source_date.desc(),
            RankSnapshotRecord.board,
            RankSnapshotRecord.dimension,
        )
        with self.database.session() as session:
            records = session.scalars(statement).all()
            active = _active_qianfan_collections(session)
            filtered = _filter_active_qianfan_snapshots(records, active)
            return [_snapshot_read(record) for record in filtered[offset : offset + limit]]

    def list_accounts(self, *, limit: int = 100, offset: int = 0) -> list[AccountRead]:
        return self._scored_accounts()[offset : offset + limit]

    def list_candidates(self, *, source_date: date, limit: int) -> list[AccountRead]:
        target = source_date.isoformat()
        accounts = self._scored_accounts(eligible_date=target)
        return accounts[:limit]

    def _scored_accounts(self, eligible_date: str | None = None) -> list[AccountRead]:
        with self.database.session() as session:
            snapshots = session.scalars(
                select(RankSnapshotRecord).options(selectinload(RankSnapshotRecord.items))
            ).all()
            snapshots = _filter_active_qianfan_snapshots(
                snapshots, _active_qianfan_collections(session)
            )

        by_user: dict[str, list[tuple[RankSnapshotRecord, RankItemRecord]]] = {}
        eligible_users: set[str] = set()
        for snapshot in snapshots:
            if eligible_date is not None and snapshot.source_date > eligible_date:
                continue
            for item in snapshot.items:
                if not item.user_id:
                    continue
                by_user.setdefault(item.user_id, []).append((snapshot, item))
                if snapshot.source_date == eligible_date:
                    eligible_users.add(item.user_id)

        rows: list[AccountRead] = []
        for user_id, evidence_rows in by_user.items():
            if eligible_date is not None and user_id not in eligible_users:
                continue
            newest_snapshot, newest_item = max(
                evidence_rows,
                key=lambda pair: (pair[0].collected_at, pair[0].id, pair[1].id),
            )
            del newest_snapshot
            score_rows = [
                {
                    "source_date": snapshot.source_date,
                    "board": snapshot.board,
                    "gmv_range": item.gmv_range,
                    "pay_rate_range": item.pay_rate_range,
                    "read_range": item.read_range,
                }
                for snapshot, item in evidence_rows
                if has_recognized_evidence(
                    {
                        "gmv_range": item.gmv_range,
                        "pay_rate_range": item.pay_rate_range,
                        "read_range": item.read_range,
                    }
                )
            ]
            fans = _latest_fans(evidence_rows)
            best_rank = min(item.rank_no for _snapshot, item in evidence_rows)
            if score_rows:
                score = score_account(score_rows, fans).model_dump()
                score_status = "scored"
            else:
                score = {
                    "score": None,
                    "evidence": 0.0,
                    "credibility": 0.0,
                    "accessibility": 0.0,
                    "fans": fans,
                    "gmv": "—",
                    "pay": "—",
                    "read": "—",
                    "nday": len({snapshot.source_date for snapshot, _item in evidence_rows}),
                    "nboard": len({snapshot.board for snapshot, _item in evidence_rows}),
                }
                score_status = "insufficient_metrics"
            rows.append(
                AccountRead(
                    user_id=user_id,
                    account_name=newest_item.author_name or user_id,
                    **score,
                    score_status=score_status,
                    ranking_evidence_count=len(evidence_rows),
                    best_rank=best_rank,
                )
            )
        rows.sort(key=lambda account: (
            account.score is None,
            -(account.score or 0.0) if account.score is not None else -account.ranking_evidence_count,
            account.best_rank,
            account.user_id,
        ))
        return rows


def _stable_key(item: RankItemInput) -> str:
    if item.note_id:
        return f"note:{item.note_id}"
    content_url = _canonical_content_url(str(item.source_url))
    if content_url is not None:
        return f"url:{content_url}"
    payload = json.dumps(
        _semantic_identity(item),
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"semantic:{sha256(payload.encode('utf-8')).hexdigest()}"


def _upsert_snapshot(
    session: Any,
    snapshot: RankSnapshotInput,
    chosen: list[tuple[str, RankItemInput]],
) -> RankSnapshotRecord:
    source_date = snapshot.source_date.isoformat()
    record = session.scalar(
        select(RankSnapshotRecord)
        .options(selectinload(RankSnapshotRecord.items))
        .where(
            RankSnapshotRecord.source_date == source_date,
            RankSnapshotRecord.board == snapshot.board,
            RankSnapshotRecord.dimension == snapshot.dimension,
        )
    )
    if record is None:
        record = RankSnapshotRecord(
            source_date=source_date,
            collected_at=snapshot.collected_at.isoformat(),
            board=snapshot.board,
            dimension=snapshot.dimension,
            source_url=str(snapshot.source_url),
            raw_evidence=snapshot.raw_evidence,
            submitted_count=len(snapshot.items),
        )
        session.add(record)
    else:
        record.items.clear()
        session.flush()
        record.collected_at = snapshot.collected_at.isoformat()
        record.source_url = str(snapshot.source_url)
        record.raw_evidence = snapshot.raw_evidence
        record.submitted_count = len(snapshot.items)
    for stable_key, item in chosen:
        record.items.append(_item_record(stable_key, item))
    return record


def _active_qianfan_collections(session: Any) -> dict[str, str]:
    collections = session.scalars(select(QianfanCollectionRecord)).all()
    latest: dict[str, tuple[int, str]] = {}
    for collection in collections:
        if (
            collection.source_date not in latest
            or collection.id > latest[collection.source_date][0]
        ):
            latest[collection.source_date] = (
                collection.id,
                collection.collection_id,
            )
    return {source_date: value[1] for source_date, value in latest.items()}


def _filter_active_qianfan_snapshots(
    snapshots: list[RankSnapshotRecord], active: dict[str, str]
) -> list[RankSnapshotRecord]:
    filtered: list[RankSnapshotRecord] = []
    for snapshot in snapshots:
        active_collection = active.get(snapshot.source_date)
        if active_collection is None:
            filtered.append(snapshot)
            continue
        binding = snapshot.raw_evidence.get("collection_binding")
        if (
            isinstance(binding, dict)
            and binding.get("collection_id") == active_collection
        ):
            filtered.append(snapshot)
    return filtered


def _semantic_identity(item: RankItemInput) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "user_id": item.user_id,
        "title": item.title,
        "publish_date": item.publish_date,
        "author_name": item.author_name,
    }
    if not any(value for value in identity.values()):
        identity["source_url"] = str(item.source_url)
    return identity


def _canonical_content_url(source_url: str) -> str | None:
    parts = urlsplit(source_url)
    path_segments = [segment.lower() for segment in parts.path.split("/") if segment]
    has_note_identity = (
        "explore" in path_segments
        and path_segments.index("explore") + 1 < len(path_segments)
    ) or (
        "item" in path_segments
        and path_segments.index("item") + 1 < len(path_segments)
    )
    if not has_note_identity:
        return None
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
    )


def _deduplicate(items: Iterable[RankItemInput]) -> list[tuple[str, RankItemInput]]:
    chosen: dict[str, tuple[tuple[int, str], RankItemInput]] = {}
    for item in items:
        stable_key = _stable_key(item)
        canonical = json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        precedence = (item.rank_no, canonical)
        if stable_key not in chosen or precedence < chosen[stable_key][0]:
            chosen[stable_key] = (precedence, item)
    return [
        (stable_key, value[1])
        for stable_key, value in sorted(
            chosen.items(), key=lambda pair: (pair[1][0], pair[0])
        )
    ]


def _item_record(stable_key: str, item: RankItemInput) -> RankItemRecord:
    return RankItemRecord(
        stable_key=stable_key,
        rank_no=item.rank_no,
        title=item.title,
        author_name=item.author_name,
        publish_date=item.publish_date,
        read_range=item.read_range,
        click_rate_range=item.click_rate_range,
        pay_rate_range=item.pay_rate_range,
        gmv_range=item.gmv_range,
        note_id=item.note_id,
        user_id=item.user_id,
        source_url=str(item.source_url),
        raw_evidence=item.raw_evidence,
    )


def _snapshot_read(record: RankSnapshotRecord) -> RankSnapshotRead:
    items = [
        RankItemRead(
            rank_no=item.rank_no,
            title=item.title,
            author_name=item.author_name,
            publish_date=item.publish_date,
            read_range=item.read_range,
            click_rate_range=item.click_rate_range,
            pay_rate_range=item.pay_rate_range,
            gmv_range=item.gmv_range,
            note_id=item.note_id,
            user_id=item.user_id,
            source_url=item.source_url,
            raw_evidence=dict(item.raw_evidence),
        )
        for item in record.items
    ]
    return RankSnapshotRead(
        id=record.id,
        source_date=record.source_date,
        collected_at=record.collected_at,
        board=record.board,
        dimension=record.dimension,
        source_url=record.source_url,
        raw_evidence=dict(record.raw_evidence),
        submitted_count=record.submitted_count,
        deduplicated_count=len(items),
        items=items,
    )


def _latest_fans(rows: list[tuple[RankSnapshotRecord, RankItemRecord]]) -> int:
    newest_first = sorted(
        rows,
        key=lambda pair: (pair[0].collected_at, pair[0].id, pair[1].id),
        reverse=True,
    )
    for _, item in newest_first:
        value = _fan_value(item.raw_evidence)
        if value is not None:
            return value
    return 0


def _fan_value(raw_evidence: dict[str, Any]) -> int | None:
    value = raw_evidence.get("userFansNum")
    if value is None and isinstance(raw_evidence.get("raw_text"), str):
        try:
            decoded = json.loads(raw_evidence["raw_text"])
        except (TypeError, ValueError):
            decoded = {}
        if isinstance(decoded, dict):
            value = decoded.get("userFansNum")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
