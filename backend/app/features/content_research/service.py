"""Transactional service for the tutorial's Phase-D research entry path."""

from __future__ import annotations

from datetime import UTC, datetime
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.adapters.contracts import ModelAdapter, ModelAdapterError, StructuredModelRequest
from backend.app.db import Database
from backend.app.features.content_research.models import (
    FinishedProductDossierRecord,
    KeywordPlanRecord,
    KeywordPlanRunRecord,
)
from backend.app.features.content_research.schemas import (
    FinishedProductDossierCreate,
    FinishedProductDossierRead,
    GeneratedKeywordPlanOutput,
    KeywordPlanItemCreate,
    KeywordPlanItemRead,
    KeywordPlanRead,
    KeywordPlanReplace,
)


KEYWORD_PROMPT_VERSION = "tutorial-content-keyword-layout-v1"
KEYWORD_SYSTEM_PROMPT = """You are the keyword-layout agent for a Xiaohongshu content research system.
The product already exists. Do not redesign it, invent features, prices, outcomes, reviews, audiences, or claims.
Use only the supplied finished-product dossier facts.
Your output is a benchmark-search keyword network, not final copy and not SEO keyword stuffing.
Return 10 to 20 distinct search terms spanning useful combinations of: main/category, positioning, visual, audience, pain/emotion, scenario, selling point, question/decision, and comparison/alternative terms.
Set expand=true only for at most three short main/category terms that are suitable for A-Z expansion; all other terms must use expand=false.
Use scope="benchmark" for every item.
Use target_count from 5 through 10, representing how many qualifying benchmark notes should be collected for that actual search term.
Do not include medical, legal, financial, efficacy, price, or other product claims unless they are explicitly allowed by the dossier.
"""


class ContentResearchError(RuntimeError):
    pass


class ContentResearchNotFound(ContentResearchError):
    pass


class ContentResearchConflict(ContentResearchError):
    pass


class ContentResearchModelUnavailable(ContentResearchError):
    pass


class ContentResearchModelFailure(ContentResearchError):
    pass


class ContentResearchService:
    def __init__(self, database: Database, model_adapter: ModelAdapter | None = None) -> None:
        self.database = database
        self.model_adapter = model_adapter

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
        self._require_dossier(dossier_id)
        return self._persist_keyword_run(
            dossier_id=dossier_id,
            items=list(payload.items),
            source="manual",
            provider=None,
            model=None,
            prompt_version=None,
            usage={},
            duration_ms=None,
        )

    def generate_keyword_plan(self, dossier_id: str) -> KeywordPlanRead:
        dossier = self._require_dossier(dossier_id)
        if self.model_adapter is None:
            raise ContentResearchModelUnavailable("Content research model is unavailable.")
        user_prompt = json.dumps(
            {
                "finished_product_dossier": {
                    "id": dossier.id,
                    "product_key": dossier.product_key,
                    "name": dossier.name,
                    "version": dossier.version,
                    "target_user": dossier.target_user,
                    "core_need": dossier.core_need,
                    "deliverables": dossier.deliverables_json,
                    "usage_instructions": dossier.usage_instructions,
                    "faq": dossier.faq_json,
                    "allowed_claims": dossier.allowed_claims_json,
                    "forbidden_claims": dossier.forbidden_claims_json,
                    "source_index": dossier.source_index_json,
                    "uat_status": dossier.uat_status,
                }
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request = StructuredModelRequest(
            system_prompt=KEYWORD_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            prompt_version=KEYWORD_PROMPT_VERSION,
            evidence_ids=[f"finished-product-dossier:{dossier.id}"],
        )
        try:
            result = self.model_adapter.generate_structured(
                request, GeneratedKeywordPlanOutput
            )
            output = GeneratedKeywordPlanOutput.model_validate(result.output)
        except ModelAdapterError as error:
            if getattr(error, "category", None) == "model_unconfigured":
                raise ContentResearchModelUnavailable(str(error)) from error
            raise ContentResearchModelFailure(str(error)) from error
        except Exception as error:
            raise ContentResearchModelFailure(
                "Keyword layout output failed validation."
            ) from error
        self._validate_generated_keyword_rules(output.items)
        return self._persist_keyword_run(
            dossier_id=dossier_id,
            items=list(output.items),
            source="ai",
            provider=getattr(self.model_adapter, "provider", "unknown"),
            model=result.model,
            prompt_version=KEYWORD_PROMPT_VERSION,
            usage={key: int(value) for key, value in result.usage.items()},
            duration_ms=result.duration_ms,
        )

    def get_keyword_plan(self, dossier_id: str) -> KeywordPlanRead:
        self._require_dossier(dossier_id)
        with self.database.session() as session:
            run = session.scalars(
                select(KeywordPlanRunRecord)
                .where(KeywordPlanRunRecord.dossier_id == dossier_id)
                .order_by(
                    KeywordPlanRunRecord.created_at.desc(),
                    KeywordPlanRunRecord.id.desc(),
                )
                .limit(1)
            ).first()
            if run is None:
                return _empty_keyword_plan(dossier_id)
            records = session.scalars(
                select(KeywordPlanRecord)
                .where(KeywordPlanRecord.run_id == run.id)
                .order_by(KeywordPlanRecord.position)
            ).all()
            return _keyword_plan_read(run, records)

    def list_keyword_plan_runs(self, dossier_id: str) -> list[KeywordPlanRead]:
        self._require_dossier(dossier_id)
        with self.database.session() as session:
            runs = session.scalars(
                select(KeywordPlanRunRecord)
                .where(KeywordPlanRunRecord.dossier_id == dossier_id)
                .order_by(
                    KeywordPlanRunRecord.created_at.desc(),
                    KeywordPlanRunRecord.id.desc(),
                )
            ).all()
            result: list[KeywordPlanRead] = []
            for run in runs:
                records = session.scalars(
                    select(KeywordPlanRecord)
                    .where(KeywordPlanRecord.run_id == run.id)
                    .order_by(KeywordPlanRecord.position)
                ).all()
                result.append(_keyword_plan_read(run, records))
            return result

    def _require_dossier(self, dossier_id: str) -> FinishedProductDossierRecord:
        with self.database.session() as session:
            record = session.get(FinishedProductDossierRecord, dossier_id)
            if record is None:
                raise ContentResearchNotFound(
                    f"Finished product dossier {dossier_id} does not exist."
                )
            session.expunge(record)
            return record

    def _persist_keyword_run(
        self,
        *,
        dossier_id: str,
        items: list[KeywordPlanItemCreate],
        source: str,
        provider: str | None,
        model: str | None,
        prompt_version: str | None,
        usage: dict[str, int],
        duration_ms: int | None,
    ) -> KeywordPlanRead:
        created_at = _now()
        with self.database.session() as session:
            if session.get(FinishedProductDossierRecord, dossier_id) is None:
                raise ContentResearchNotFound(
                    f"Finished product dossier {dossier_id} does not exist."
                )
            run = KeywordPlanRunRecord(
                dossier_id=dossier_id,
                source=source,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                usage_json=dict(usage),
                duration_ms=duration_ms,
                created_at=created_at,
            )
            session.add(run)
            session.flush()
            records = [
                KeywordPlanRecord(
                    run_id=run.id,
                    position=position,
                    keyword=item.keyword,
                    category=item.category,
                    expand=item.expand,
                    scope=item.scope,
                    target_count=item.target_count,
                    created_at=created_at,
                )
                for position, item in enumerate(items, start=1)
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
                .where(KeywordPlanRecord.run_id == run.id)
                .order_by(KeywordPlanRecord.position)
            ).all()
            return _keyword_plan_read(run, persisted)

    @staticmethod
    def _validate_generated_keyword_rules(items: list[KeywordPlanItemCreate]) -> None:
        expanded = [item for item in items if item.expand]
        if len(expanded) > 3 or any(item.category != "main" for item in expanded):
            raise ContentResearchModelFailure(
                "AI keyword layout violated the bounded expansion rule."
            )
        if any(item.scope != "benchmark" for item in items):
            raise ContentResearchModelFailure(
                "AI keyword layout used an unsupported collection scope."
            )
        if any(not 5 <= item.target_count <= 10 for item in items):
            raise ContentResearchModelFailure(
                "AI keyword layout used an unsupported benchmark target count."
            )


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
    run: KeywordPlanRunRecord, records: list[KeywordPlanRecord]
) -> KeywordPlanRead:
    items = [
        KeywordPlanItemRead(
            id=record.id,
            run_id=record.run_id,
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
    return KeywordPlanRead(
        dossier_id=run.dossier_id,
        run_id=run.id,
        source=run.source,
        provider=run.provider,
        model=run.model,
        prompt_version=run.prompt_version,
        usage={key: int(value) for key, value in run.usage_json.items()},
        duration_ms=run.duration_ms,
        created_at=run.created_at,
        count=len(items),
        items=items,
    )


def _empty_keyword_plan(dossier_id: str) -> KeywordPlanRead:
    return KeywordPlanRead(
        dossier_id=dossier_id,
        run_id=None,
        source=None,
        provider=None,
        model=None,
        prompt_version=None,
        usage={},
        duration_ms=None,
        created_at=None,
        count=0,
        items=[],
    )


def _now() -> datetime:
    return datetime.now(UTC)
