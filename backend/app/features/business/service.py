"""Business-first read projection over existing durable opportunity/product truth."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.db import Database
from backend.app.features.analysis.models import OpportunityRecord
from backend.app.features.content.models import ProductRecord
from backend.app.features.business.schemas import (
    DemandRadarDirectionRead,
    DemandRadarLinkedProductRead,
    DemandRadarProductRead,
    DemandRadarRead,
    DemandRadarSummaryRead,
)


class BusinessWorkbenchService:
    def __init__(self, database: Database, *, today: Callable[[], date] = date.today) -> None:
        self.database = database
        self.today = today

    def demand_radar(self) -> DemandRadarRead:
        generated_on = self.today()
        with self.database.session() as session:
            opportunities = list(
                session.scalars(
                    select(OpportunityRecord)
                    .options(selectinload(OpportunityRecord.analysis))
                    .order_by(OpportunityRecord.created_at.desc(), OpportunityRecord.id)
                )
            )
            products = list(
                session.scalars(
                    select(ProductRecord).order_by(ProductRecord.created_at, ProductRecord.id)
                )
            )

        products_by_opportunity: dict[str, list[ProductRecord]] = defaultdict(list)
        for product in products:
            products_by_opportunity[product.opportunity_id].append(product)

        directions = [
            self._direction(
                opportunity,
                linked_products=products_by_opportunity.get(opportunity.id, []),
                generated_on=generated_on,
            )
            for opportunity in opportunities
        ]
        directions.sort(key=_direction_sort_key)

        return DemandRadarRead(
            summary=DemandRadarSummaryRead(
                generated_on=generated_on,
                total_direction_count=len(directions),
                new_today_count=sum(item.is_new_today for item in directions),
                warming_count=sum(
                    item.evidence_level == "warming_candidate"
                    and item.review_status != "rejected"
                    for item in directions
                ),
                validated_count=sum(
                    item.evidence_level == "validated_candidate"
                    and item.review_status != "rejected"
                    for item in directions
                ),
                pending_decision_count=sum(
                    item.review_status == "pending_review" for item in directions
                ),
                approved_count=sum(item.review_status == "approved" for item in directions),
                linked_product_count=sum(len(item.linked_products) for item in directions),
            ),
            directions=directions,
        )

    def _direction(
        self,
        opportunity: OpportunityRecord,
        *,
        linked_products: list[ProductRecord],
        generated_on: date,
    ) -> DemandRadarDirectionRead:
        representative_products = _representative_products(opportunity)
        if opportunity.review_status == "rejected":
            journey_stage = "closed"
            next_business_action = "这个方向已经被拒绝；保留证据用于审计，不进入产品定义。"
        elif opportunity.review_status == "pending_review":
            journey_stage = "demand_decision"
            next_business_action = "查看支撑账号、商品和图片证据后，决定跟进、继续观察或拒绝。"
        elif linked_products:
            # Existing ProductRecord belongs to the pre-Stage-7 content model.  It
            # proves that a product workspace exists, but not that the new
            # Product Definition Gate has been implemented or approved.
            journey_stage = "legacy_product_workspace"
            next_business_action = "已有历史产品工作区；进入 Stage 7 产品定义前先核对现有产品资料。"
        else:
            journey_stage = "product_definition"
            next_business_action = "方向已批准；下一步进入产品研究并形成 Product Definition 草案。"

        return DemandRadarDirectionRead(
            opportunity_id=opportunity.id,
            title=opportunity.title,
            summary=opportunity.summary,
            evidence_level=opportunity.evidence_level,
            review_status=opportunity.review_status,
            can_follow_up=_has_positive_shared_demand(opportunity),
            journey_stage=journey_stage,
            is_new_today=opportunity.created_at.date() == generated_on,
            supporting_account_count=len(opportunity.supporting_accounts_json or []),
            supporting_product_count=len(opportunity.supporting_products_json or []),
            supporting_note_count=len(opportunity.supporting_notes_json or []),
            image_evidence_count=sum(
                max(0, int(item.get("image_evidence_count") or 0))
                for item in opportunity.supporting_products_json or []
                if isinstance(item, dict)
            ),
            representative_products=representative_products,
            linked_products=[
                DemandRadarLinkedProductRead(
                    product_id=product.id,
                    name=product.name,
                    target_user=product.target_user,
                    created_at=product.created_at,
                )
                for product in linked_products
            ],
            next_business_action=next_business_action,
            created_at=opportunity.created_at,
            reviewed_at=opportunity.reviewed_at,
        )


def _representative_products(opportunity: OpportunityRecord) -> list[DemandRadarProductRead]:
    lookup = _snapshot_product_facts(opportunity)
    rows: list[DemandRadarProductRead] = []
    for item in (opportunity.supporting_products_json or [])[:3]:
        if not isinstance(item, dict):
            continue
        source_url = str(item.get("source_url") or "")
        product_id = str(item.get("product_id") or "")
        account_user_id = str(item.get("account_user_id") or "")
        observed = lookup.get((str(item.get("evidence_id") or ""), product_id, source_url), {})
        rows.append(
            DemandRadarProductRead(
                account_user_id=account_user_id,
                product_id=product_id,
                title=item.get("title") if isinstance(item.get("title"), str) else None,
                source_url=source_url,
                price=_safe_text(observed.get("price")),
                sold=_safe_text(observed.get("sold")),
                image_evidence_count=max(0, int(item.get("image_evidence_count") or 0)),
            )
        )
    return rows


def _snapshot_product_facts(opportunity: OpportunityRecord) -> dict[tuple[str, str, str], dict[str, Any]]:
    snapshot = opportunity.analysis.evidence_snapshot_json if opportunity.analysis is not None else None
    facts = snapshot.get("facts") if isinstance(snapshot, dict) else None
    if not isinstance(facts, list):
        return {}
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        evidence_id = fact.get("evidence_id")
        trusted = fact.get("trusted_shop_result")
        items = trusted.get("items") if isinstance(trusted, dict) else None
        if not isinstance(evidence_id, str) or not isinstance(items, list):
            continue
        for product in items:
            if not isinstance(product, dict):
                continue
            data = product.get("data") if isinstance(product.get("data"), dict) else {}
            raw = product.get("raw_evidence") if isinstance(product.get("raw_evidence"), dict) else {}
            product_id = str(product.get("id") or "")
            source_url = str(product.get("source_url") or "")
            result[(evidence_id, product_id, source_url)] = {
                "price": data.get("price") or raw.get("price"),
                "sold": data.get("sold") or raw.get("sold"),
            }
    return result


def _safe_text(value: object) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    rendered = str(value).strip()
    return rendered[:200] if rendered else None


def _has_positive_shared_demand(opportunity: OpportunityRecord) -> bool:
    output = opportunity.analysis.output_json if opportunity.analysis is not None else None
    conclusion = output.get("cross_account_conclusion") if isinstance(output, dict) else None
    return bool(
        isinstance(conclusion, dict)
        and conclusion.get("has_specific_shared_demand") is True
    )


def _direction_sort_key(item: DemandRadarDirectionRead) -> tuple[int, int, float, str]:
    review_priority = {"pending_review": 0, "approved": 1, "rejected": 2}[item.review_status]
    evidence_priority = {
        "validated_candidate": 0,
        "warming_candidate": 1,
        "legacy_ungraded": 2,
    }[item.evidence_level]
    return (review_priority, evidence_priority, -item.created_at.timestamp(), item.opportunity_id)
