"""Conservative public-evidence scope prescreen before Android shop work."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PublicScopeFact(BaseModel):
    evidence_id: str = Field(min_length=1, max_length=100)
    kind: Literal["rank_item", "account_profile", "account_note"]
    account_user_id: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=4000)
    source_url: str = Field(min_length=1, max_length=2000)
    raw_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CandidatePrescreenDecision(BaseModel):
    classification: Literal["likely_digital", "clearly_physical", "uncertain"]
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(min_length=1, max_length=50)


_PHYSICAL_PRODUCT_TERMS = (
    "服装", "衣服", "上衣", "裤子", "裙子", "鞋子", "鞋靴", "箱包", "包包",
    "食品", "零食", "饮料", "餐具", "锅具", "饰品", "项链", "耳环", "日用品",
    "家居", "家具", "家电", "护肤品", "美妆", "手机壳", "耳机", "玩具", "文具",
    "实体配件", "实物商品",
)
_PHYSICAL_FULFILLMENT_TERMS = (
    "快递", "包邮", "运费", "物流", "发货", "现货", "预售", "到货", "尺码",
    "退换", "保修", "下单",
)
_DIGITAL_TERMS = (
    "pdf", "ppt", "word", "excel", "电子资料", "电子版", "网盘", "下载", "模板",
    "教程", "课程资料", "题库", "素材包", "提示词", "源码", "软件", "小程序", "网站",
    "本地工具", "数字服务", "数字权益", "电子优惠券", "优惠券", "兑换码", "会员权益",
    "无需物流", "在线课程",
)


def classify_candidate_scope(
    facts: list[PublicScopeFact],
) -> CandidatePrescreenDecision:
    if not facts:
        raise ValueError("candidate prescreen requires persisted public evidence")
    physical_refs: list[str] = []
    fulfillment_refs: list[str] = []
    digital_refs: list[str] = []
    for fact in facts:
        corpus = fact.text.casefold()
        if any(term.casefold() in corpus for term in _PHYSICAL_PRODUCT_TERMS):
            physical_refs.append(fact.evidence_id)
        if any(term.casefold() in corpus for term in _PHYSICAL_FULFILLMENT_TERMS):
            fulfillment_refs.append(fact.evidence_id)
        if any(term.casefold() in corpus for term in _DIGITAL_TERMS):
            digital_refs.append(fact.evidence_id)
    physical = list(dict.fromkeys([*physical_refs, *fulfillment_refs]))
    digital = list(dict.fromkeys(digital_refs))
    if physical and digital:
        return CandidatePrescreenDecision(
            classification="uncertain",
            reason="公开证据同时出现实体和数字交付线索，必须继续 Android preflight。",
            evidence_ids=list(dict.fromkeys([*physical, *digital])),
        )
    if physical_refs and (fulfillment_refs or len(set(physical_refs)) >= 2):
        return CandidatePrescreenDecision(
            classification="clearly_physical",
            reason="公开证据明确显示实体商品及物流、发货或多条实体商品线索。",
            evidence_ids=physical,
        )
    if digital:
        return CandidatePrescreenDecision(
            classification="likely_digital",
            reason="公开证据出现明确数字或虚拟交付线索；仍需 Android preflight 最终核验。",
            evidence_ids=digital,
        )
    return CandidatePrescreenDecision(
        classification="uncertain",
        reason="现有公开证据不足以可靠判断业务范围，必须继续 Android preflight。",
        evidence_ids=[fact.evidence_id for fact in facts],
    )
