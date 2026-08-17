"""Transactional ranking ingestion, deterministic deduplication and account scoring."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from hashlib import sha256
from typing import Any

from sqlalchemy import select
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
)
from backend.app.features.radar.scoring import score_account


class RadarService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ingest_snapshot(self, snapshot: RankSnapshotInput) -> RankSnapshotRead:
        chosen = _deduplicate(snapshot.items)
        source_date = snapshot.source_date.isoformat()
        with self.database.session() as session:
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
            session.commit()
            session.refresh(record)
            return _snapshot_read(record)

    def list_snapshots(self, *, source_date: str | date | None = None) -> list[RankSnapshotRead]:
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
            return [_snapshot_read(record) for record in session.scalars(statement).all()]

    def list_accounts(self) -> list[AccountRead]:
        return self._scored_accounts()

    def list_candidates(self, *, source_date: date, limit: int) -> list[AccountRead]:
        target = source_date.isoformat()
        accounts = self._scored_accounts(eligible_date=target)
        return accounts[:limit]

    def _scored_accounts(self, eligible_date: str | None = None) -> list[AccountRead]:
        with self.database.session() as session:
            snapshots = session.scalars(
                select(RankSnapshotRecord).options(selectinload(RankSnapshotRecord.items))
            ).all()

        by_user: dict[str, list[tuple[RankSnapshotRecord, RankItemRecord]]] = {}
        eligible_users: set[str] = set()
        for snapshot in snapshots:
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
                if item.gmv_range is not None
            ]
            if not score_rows:
                continue
            fans = _latest_fans(evidence_rows)
            score = score_account(score_rows, fans)
            rows.append(
                AccountRead(
                    user_id=user_id,
                    account_name=newest_item.author_name or user_id,
                    **score.model_dump(),
                )
            )
        rows.sort(key=lambda account: (-account.score, account.user_id))
        return rows


def _stable_key(item: RankItemInput) -> str:
    if item.note_id:
        return f"note:{item.note_id}"
    if item.user_id:
        return f"user:{item.user_id}"
    payload = json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return f"raw:{sha256(payload.encode('utf-8')).hexdigest()}"


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
