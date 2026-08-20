"""Small, deterministic shop-scope gate used before expensive product reads."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.adapters.contracts import StructuredModelRequest


ShopScopeClassification = Literal[
    "in_scope", "out_of_scope_physical", "needs_human"
]

_PHYSICAL_TERMS = (
    "快递",
    "包邮",
    "运费",
    "发货",
    "预售",
    "到货",
    "七天无理由",
    "退换",
    "保修",
    "尺码",
    "颜色规格",
    "实物包装",
    "配件",
    "重量",
    "尺寸",
    "实物商品",
    "服装",
    "连衣裙",
    "衬衫",
    "半身裙",
    "裤子",
    "鞋子",
    "箱包",
    "首饰",
    "美妆",
    "护肤",
    "护肤品",
    "食品",
    "零食",
    "家居",
    "家具",
    "家电",
    "猫猫枕",
    "按摩枕",
    "筋膜枪",
    "按摩器",
    "按摩仪",
    "猫窝",
    "手机壳",
    "耳机",
    "玩具",
    "文具",
    "运动用品",
    "汽车用品",
)
_DIGITAL_TERMS = (
    "pdf",
    "ppt",
    "word",
    "excel",
    "canva模板",
    "模板文件",
    "电子版",
    "网盘",
    "下载",
    "源码",
    "软件",
    "数字内容",
    "数字服务",
    "数字资料",
    "数字教程",
    "虚拟资料",
    "无需物流",
    "数字文件交付",
    "网站",
    "小程序",
    "本地工具",
)


class ShopScopeEvidence(BaseModel):
    shop_profile: str = Field(min_length=1, max_length=1000)
    representative_product_titles: list[str] = Field(min_length=1, max_length=3)
    visible_descriptions: list[str] = Field(default_factory=list, max_length=3)
    account_profile: str | None = Field(default=None, max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)

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

    @field_validator("visible_descriptions", "evidence_refs")
    @classmethod
    def trim_optional_lists(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("scope evidence lists must contain distinct non-empty text")
        return normalized


class ShopScopeModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: Literal["physical", "digital", "ambiguous"]
    reason: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)

    @field_validator("evidence_refs")
    @classmethod
    def distinct_refs(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("model evidence refs must be distinct non-empty text")
        return normalized


class ShopScopeDecision(BaseModel):
    classification: ShopScopeClassification
    reason: str = Field(min_length=1, max_length=500)
    representative_product_count: int = Field(ge=1, le=3)
    deep_collection_allowed: bool
    decision_source: Literal["rule", "model", "human"] = "rule"
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    evidence: ShopScopeEvidence


def classify_shop_scope(
    evidence: ShopScopeEvidence, *, model_adapter: Any | None = None
) -> ShopScopeDecision:
    corpus = " ".join(
        [
            evidence.shop_profile,
            evidence.account_profile or "",
            *evidence.representative_product_titles,
            *evidence.visible_descriptions,
        ]
    ).casefold()
    physical = any(term.casefold() in corpus for term in _PHYSICAL_TERMS)
    digital = any(term.casefold() in corpus for term in _DIGITAL_TERMS)
    if physical and digital:
        classification: ShopScopeClassification = "needs_human"
        reason = "scope_evidence_conflict"
        decision_source: Literal["rule", "model", "human"] = "rule"
        refs = list(evidence.evidence_refs)
    elif physical:
        classification: ShopScopeClassification = "out_of_scope_physical"
        reason = "physical_goods_detected"
        decision_source = "rule"
        refs = list(evidence.evidence_refs)
    elif digital:
        classification = "in_scope"
        reason = "digital_delivery_evidence_detected"
        decision_source = "rule"
        refs = list(evidence.evidence_refs)
    elif model_adapter is not None and evidence.evidence_refs:
        try:
            generated = model_adapter.generate_structured(
                StructuredModelRequest(
                    system_prompt=(
                        "Classify only whether the cited real shop evidence proves physical "
                        "delivery, digital delivery, or remains ambiguous. Physical and digital "
                        "decisions must cite the exact supplied evidence refs. Absence of a "
                        "physical keyword is never digital evidence."
                    ),
                    user_prompt=json.dumps(
                        evidence.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    prompt_version="shop-scope-v1",
                    evidence_ids=list(evidence.evidence_refs),
                ),
                ShopScopeModelOutput,
            )
            model_output = ShopScopeModelOutput.model_validate(generated.output)
            if not set(model_output.evidence_refs).issubset(evidence.evidence_refs):
                raise ValueError("model cited evidence outside the preflight scope")
        except Exception:
            classification = "needs_human"
            reason = "scope_model_unavailable"
            decision_source = "rule"
            refs = list(evidence.evidence_refs)
        else:
            classification = {
                "physical": "out_of_scope_physical",
                "digital": "in_scope",
                "ambiguous": "needs_human",
            }[model_output.classification]
            reason = model_output.reason
            decision_source = "model"
            refs = list(model_output.evidence_refs)
    else:
        classification = "needs_human"
        reason = "scope_evidence_ambiguous"
        decision_source = "rule"
        refs = list(evidence.evidence_refs)
    return ShopScopeDecision(
        classification=classification,
        reason=reason,
        representative_product_count=len(evidence.representative_product_titles),
        deep_collection_allowed=classification == "in_scope",
        decision_source=decision_source,
        evidence_refs=refs,
        evidence=evidence,
    )
