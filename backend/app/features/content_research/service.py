"""Transactional service for the tutorial's Phase-D research entry path."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from backend.app.db import Database
from backend.app.features.content_research.models import (
    FinishedProductDossierRecord,
    KeywordPlanRecord,
)
from backend.app.features.content_research.schemas import (
    FinishedProductDossierCreate,
    FinishedProductDossierRead,
    KeywordPlanItemRead,
    KeywordPlanRead,
    KeywordPlanReplace,
)


class ContentResearchError(RuntimeError):
    pass


class ContentResearchNotFound(ContentResearchError):
    pass


class ContentResearchConflict(ContentResearchError):
    pass


class ContentResearchService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_dossier(self, payload: FinishedProductDossierCreate) -> FinishedProductDossierRead:
        record = FinishedProductDossierRecord(
            product_key=payload.product_key,
            name=payload.name,
            version=payload.version,
            target_user=payload.target_user,
            core_need=payload.core_need,
            deliverables_json=list(payload.deliverables),
            usage_instructions=payload.usage_instructions,
            faq_json=[entry.model_dump(mode="json") for entry in payload.faq],
            allowed_claims_json=list(payload.allowed_claims),
            forbidden_claims_json=list(payload.forbidden_claims),
            source_index_json=list(payload.source_index),
            uat_status=payload.uat_status,
            created_at=_now(),
        )
        with self.database.session() as session:
            session.add(record)
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise ContentResearchConflict(
                    "This finished-product key and version already exist."
                ) from error
            session.refresh(record)
            return _dossier_read(record)

    def list_dossiers(self) -> list[FinishedProductDossierRead]:
        with self.database.session() as session:
            records = session.scalars(
                select(FinishedProductDossierRecord).order_by(
                    FinishedProductDossierRecord.created_at,
                    FinishedProductDossierRecord.id,
                )
            ).all()
            return [_dossier_read(record) for record in records]

    def get_dossier(self, dossier_id: str) -> FinishedProductDossierRead:
        with self.database.session() as session:
            record = session.get(FinishedProductDossierRecord, dossier_id)
            if record is None:
                raise ContentResearchNotFound(
                    f"Finished product dossier {dossier_id} does not exist."
                )
            return _dossier_read(record)

    def replace_keyword_plan(
        self, dossier_id: str, payload: KeywordPlanReplace
    ) -> KeywordPlanRead:
        created_at = _now()
        with self.database.session() as session:
            dossier = session.get(FinishedProductDossierRecord, dossier_id)
            if dossier is None:
                raise ContentResearchNotFound(
                    f"Finished product dossier {dossier_id} does not exist."
                )
            session.execute(
                delete(KeywordPlanRecord).where(
                    KeywordPlanRecord.dossier_id == dossier_id
                )
            )
            records = [
                KeywordPlanRecord(
                    dossier_id=dossier_id,
                    position=position,
                    keyword=item.keyword,
                    category=item.category,
                    expand=item.expand,
                    scope=item.scope,
                    target_count=item.target_count,
                    created_at=created_at,
                )
                for position, item in enumerate(payload.items, start=1)
            ]
            session.add_all(records)
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise ContentResearchConflict(
                    "Keyword plan conflicts with persisted product research facts."
                ) from error
            persisted = session.scalars(
                select(KeywordPlanRecord)
                .where(KeywordPlanRecord.dossier_id == dossier_id)
                .order_by(KeywordPlanRecord.position)
            ).all()
            return _keyword_plan_read(dossier_id, persisted)

    def get_keyword_plan(self, dossier_id: str) -> KeywordPlanRead:
        with self.database.session() as session:
            if session.get(FinishedProductDossierRecord, dossier_id) is None:
                raise ContentResearchNotFound(
                    f"Finished product dossier {dossier_id} does not exist."
                )
            records = session.scalars(
                select(KeywordPlanRecord)
                .where(KeywordPlanRecord.dossier_id == dossier_id)
                .order_by(KeywordPlanRecord.position)
            ).all()
            return _keyword_plan_read(dossier_id, records)


def _dossier_read(record: FinishedProductDossierRecord) -> FinishedProductDossierRead:
    return FinishedProductDossierRead(
        id=record.id,
        product_key=record.product_key,
        name=record.name,
        version=record.version,
        target_user=record.target_user,
        core_need=record.core_need,
        deliverables=list(record.deliverables_json),
        usage_instructions=record.usage_instructions,
        faq=list(record.faq_json),
        allowed_claims=list(record.allowed_claims_json),
        forbidden_claims=list(record.forbidden_claims_json),
        source_index=list(record.source_index_json),
        uat_status=record.uat_status,
        created_at=record.created_at,
    )


def _keyword_plan_read(
    dossier_id: str, records: list[KeywordPlanRecord]
) -> KeywordPlanRead:
    items = [
        KeywordPlanItemRead(
            id=record.id,
            dossier_id=record.dossier_id,
            position=record.position,
            keyword=record.keyword,
            category=record.category,
            expand=record.expand,
            scope=record.scope,
            target_count=record.target_count,
            created_at=record.created_at,
        )
        for record in records
    ]
    return KeywordPlanRead(dossier_id=dossier_id, count=len(items), items=items)


def _now() -> datetime:
    return datetime.now(UTC)
