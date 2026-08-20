"""Small, deterministic shop-scope gate used before expensive product reads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


ShopScopeClassification = Literal[
    "in_scope", "out_of_scope_physical", "needs_human"
]

_PHYSICAL_TERMS = (
    "服装",
    "连衣裙",
    "衬衫",
    "半身裙",
    "美妆",
    "护肤",
    "食品",
    "零食",
    "家居",
    "家具",
    "物流",
    "发货",
    "库存",
)
_DIGITAL_TERMS = (
    "资料",
    "模板",
    "教程",
    "测试",
    "数字内容",
    "数字服务",
    "网站",
    "小程序",
    "本地工具",
)


class ShopScopeEvidence(BaseModel):
    shop_profile: str = Field(min_length=1, max_length=1000)
    representative_product_titles: list[str] = Field(min_length=1, max_length=3)

    @field_validator("shop_profile")
    @classmethod
    def trim_profile(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("shop_profile must contain non-whitespace text")
        return normalized

    @field_validator("representative_product_titles", mode="before")
    @classmethod
    def bound_representative_products(cls, value: object) -> object:
        if isinstance(value, list):
            return value[:3]
        return value

    @field_validator("representative_product_titles")
    @classmethod
    def trim_titles(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("representative titles must contain non-whitespace text")
        return normalized


class ShopScopeDecision(BaseModel):
    classification: ShopScopeClassification
    reason: Literal[
        "physical_goods_detected",
        "digital_delivery_evidence_detected",
        "scope_evidence_ambiguous",
    ]
    representative_product_count: int = Field(ge=1, le=3)
    deep_collection_allowed: bool
    evidence: ShopScopeEvidence


def classify_shop_scope(evidence: ShopScopeEvidence) -> ShopScopeDecision:
    corpus = " ".join(
        [evidence.shop_profile, *evidence.representative_product_titles]
    ).casefold()
    physical = any(term.casefold() in corpus for term in _PHYSICAL_TERMS)
    digital = any(term.casefold() in corpus for term in _DIGITAL_TERMS)
    if physical:
        classification: ShopScopeClassification = "out_of_scope_physical"
        reason = "physical_goods_detected"
    elif digital:
        classification = "in_scope"
        reason = "digital_delivery_evidence_detected"
    else:
        classification = "needs_human"
        reason = "scope_evidence_ambiguous"
    return ShopScopeDecision(
        classification=classification,
        reason=reason,
        representative_product_count=len(evidence.representative_product_titles),
        deep_collection_allowed=classification == "in_scope",
        evidence=evidence,
    )
