"""Literal tutorial scoring formula for evidence-backed candidate accounts."""

from collections.abc import Mapping, Sequence
from typing import Any

from backend.app.features.radar.models import AccountScore


GMV_BUCKET_SCORES = {
    "￥0-1000": 0.10,
    "￥1000-5000": 0.30,
    "5000-1万": 0.60,
    "1万-5万": 1.00,
}
PAY_BUCKET_SCORES = {
    "0-5%": 0.10,
    "5%-15%": 0.30,
    "15%-25%": 0.55,
    "25%-50%": 0.80,
    "50%-70%": 0.95,
    "70%-90%": 1.00,
}
READ_BUCKET_SCORES = {
    "1000-3000": 0.10,
    "3000-5000": 0.20,
    "5000-7000": 0.30,
    "7000-9000": 0.40,
    "9000-1万": 0.50,
    "1万-3万": 0.60,
    "3万-5万": 0.70,
    "5万-7万": 0.80,
    "7万-10万": 0.90,
    "10万以上": 1.00,
}


def _best(
    items: Sequence[Mapping[str, Any]], field: str, score_map: Mapping[str, float]
) -> tuple[str, float]:
    pool = [
        (str(item[field]), score_map[str(item[field])])
        for item in items
        if item.get(field) in score_map
    ]
    if not pool:
        return "—", 0.0
    return max(pool, key=lambda pair: pair[1])


def score_account(items: Sequence[Mapping[str, Any]], fans: int) -> AccountScore:
    """Apply the tutorial's three multiplicative components without category inference."""
    gmv_bucket, gmv = _best(items, "gmv_range", GMV_BUCKET_SCORES)
    pay_bucket, pay = _best(items, "pay_rate_range", PAY_BUCKET_SCORES)
    read_bucket, read = _best(items, "read_range", READ_BUCKET_SCORES)

    evidence = 0.50 * gmv + 0.35 * pay + 0.15 * read
    day_count = len({str(item["source_date"]) for item in items})
    board_count = len({str(item["board"]) for item in items})
    credibility = (
        1
        + 0.25 * min(day_count / 10, 1.0)
        + 0.25 * min(board_count / 4, 1.0)
    )
    low_fan = (1000 - fans) / 1000 if 0 < fans < 1000 else 0.0
    accessibility = 1 + 0.6 * low_fan

    return AccountScore(
        score=round(evidence * credibility * accessibility, 2),
        evidence=round(evidence, 2),
        credibility=round(credibility, 2),
        accessibility=round(accessibility, 2),
        fans=fans,
        gmv=gmv_bucket,
        pay=pay_bucket,
        read=read_bucket,
        nday=day_count,
        nboard=board_count,
    )
