"""Durable product materials, immutable content revisions and packages."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import ForeignKey, JSON, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


def _uuid() -> str:
    return str(uuid4())


class ProductRecord(Base):
    __tablename__ = "content_products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    target_user: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    materials: Mapped[list["ProductMaterialRecord"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductMaterialRecord.id"
    )


class ProductMaterialRecord(Base):
    __tablename__ = "content_product_materials"
    __table_args__ = (
        UniqueConstraint("product_id", "logical_name", "version", name="uq_material_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("content_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    logical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    product: Mapped[ProductRecord] = relationship(back_populates="materials")


class ContentItemRecord(Base):
    __tablename__ = "content_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("content_products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    template_key: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    material_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    research_facts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    revisions: Mapped[list["ContentRevisionRecord"]] = relationship(
        back_populates="item", cascade="all, delete-orphan",
        foreign_keys="ContentRevisionRecord.content_item_id",
        order_by="ContentRevisionRecord.number",
    )
    reviews: Mapped[list["ContentReviewRecord"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="ContentReviewRecord.id"
    )


class ContentRevisionRecord(Base):
    __tablename__ = "content_revisions"
    __table_args__ = (
        UniqueConstraint("content_item_id", "number", name="uq_content_revision_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    claims_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    source_evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    model_provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(300), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    usage_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    attempts_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    item: Mapped[ContentItemRecord] = relationship(
        back_populates="revisions", foreign_keys=[content_item_id]
    )


class ContentReviewRecord(Base):
    __tablename__ = "content_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("content_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    item: Mapped[ContentItemRecord] = relationship(back_populates="reviews")


class ContentPackageRecord(Base):
    __tablename__ = "content_packages"
    __table_args__ = (
        UniqueConstraint("revision_id", name="uq_content_package_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("content_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
