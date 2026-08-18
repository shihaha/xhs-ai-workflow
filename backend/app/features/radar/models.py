"""Persistence and API models for raw ranking snapshots and scored accounts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, Field
from sqlalchemy import ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


BoardName = Literal["阅读榜", "引流榜", "热卖榜", "成交榜"]
DimensionName = Literal["优秀内容", "优秀账号"]


class RankSnapshotRecord(Base):
    __tablename__ = "radar_rank_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "source_date", "board", "dimension", name="uq_radar_snapshot_source_scope"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    collected_at: Mapped[str] = mapped_column(String(40), nullable=False)
    board: Mapped[str] = mapped_column(String(20), nullable=False)
    dimension: Mapped[str] = mapped_column(String(20), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    raw_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    submitted_count: Mapped[int] = mapped_column(nullable=False)

    items: Mapped[list["RankItemRecord"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
        order_by="RankItemRecord.rank_no, RankItemRecord.stable_key",
    )


class QianfanCollectionRecord(Base):
    __tablename__ = "radar_qianfan_collections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    collection_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    source_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    started_at: Mapped[str] = mapped_column(String(40), nullable=False)
    expected_count_per_scope: Mapped[int] = mapped_column(nullable=False)
    selector_profile_version: Mapped[str] = mapped_column(String(100), nullable=False)


class RankItemRecord(Base):
    __tablename__ = "radar_rank_items"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "stable_key", name="uq_radar_item_snapshot_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("radar_rank_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stable_key: Mapped[str] = mapped_column(String(600), nullable=False)
    rank_no: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_date: Mapped[str | None] = mapped_column(String(40), nullable=True)
    read_range: Mapped[str | None] = mapped_column(String(50), nullable=True)
    click_rate_range: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pay_rate_range: Mapped[str | None] = mapped_column(String(50), nullable=True)
    gmv_range: Mapped[str | None] = mapped_column(String(50), nullable=True)
    note_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    raw_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    snapshot: Mapped[RankSnapshotRecord] = relationship(back_populates="items")


class RankItemInput(BaseModel):
    rank_no: int = Field(ge=1)
    title: str | None = None
    author_name: str | None = None
    publish_date: str | None = None
    read_range: str | None = None
    click_rate_range: str | None = None
    pay_rate_range: str | None = None
    gmv_range: str | None = None
    note_id: str | None = None
    user_id: str | None = None
    source_url: AnyHttpUrl
    raw_evidence: dict[str, Any] = Field(min_length=1)


class RankSnapshotInput(BaseModel):
    source_date: date
    collected_at: datetime
    board: BoardName
    dimension: DimensionName
    source_url: AnyHttpUrl
    raw_evidence: dict[str, Any] = Field(min_length=1)
    items: list[RankItemInput] = Field(min_length=1)


class RankItemRead(BaseModel):
    rank_no: int
    title: str | None
    author_name: str | None
    publish_date: str | None
    read_range: str | None
    click_rate_range: str | None
    pay_rate_range: str | None
    gmv_range: str | None
    note_id: str | None
    user_id: str | None
    source_url: str
    raw_evidence: dict[str, Any]


class RankSnapshotRead(BaseModel):
    id: int
    source_date: date
    collected_at: datetime
    board: BoardName
    dimension: DimensionName
    source_url: str
    raw_evidence: dict[str, Any]
    submitted_count: int
    deduplicated_count: int
    items: list[RankItemRead]


class AccountScore(BaseModel):
    score: float
    evidence: float
    credibility: float
    accessibility: float
    fans: int
    gmv: str
    pay: str
    read: str
    nday: int
    nboard: int


class AccountRead(AccountScore):
    user_id: str
    account_name: str
