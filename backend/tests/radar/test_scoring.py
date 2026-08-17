import json
from pathlib import Path

import pytest

from backend.app.features.radar.scoring import (
    GMV_BUCKET_SCORES,
    PAY_BUCKET_SCORES,
    READ_BUCKET_SCORES,
    score_account,
)


def test_tutorial_bucket_maps_are_preserved_as_literal_fixtures() -> None:
    """Changing any tutorial bucket value would silently change candidate ranking."""
    assert GMV_BUCKET_SCORES == {
        "￥0-1000": 0.10,
        "￥1000-5000": 0.30,
        "5000-1万": 0.60,
        "1万-5万": 1.00,
    }
    assert PAY_BUCKET_SCORES == {
        "0-5%": 0.10,
        "5%-15%": 0.30,
        "15%-25%": 0.55,
        "25%-50%": 0.80,
        "50%-70%": 0.95,
        "70%-90%": 1.00,
    }
    assert READ_BUCKET_SCORES == {
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


def test_score_uses_tutorial_product_formula_and_explains_components() -> None:
    """Replacing multiplication or a weight with a plausible alternative must fail."""
    result = score_account(
        [
            {
                "source_date": "2026-08-17",
                "board": "成交榜",
                "gmv_range": "￥1000-5000",
                "pay_rate_range": "15%-25%",
                "read_range": "3万-5万",
            }
        ],
        fans=250,
    )

    assert result.model_dump() == {
        "score": 0.71,
        "evidence": 0.45,
        "credibility": 1.09,
        "accessibility": 1.45,
        "fans": 250,
        "gmv": "￥1000-5000",
        "pay": "15%-25%",
        "read": "3万-5万",
        "nday": 1,
        "nboard": 1,
    }


def test_score_caps_credibility_and_only_boosts_positive_sub_thousand_fans() -> None:
    """Unknown/zero fans and excess days or boards must not inflate the tutorial score."""
    items = [
        {
            "source_date": f"2026-08-{day:02d}",
            "board": f"榜-{day}",
            "gmv_range": "1万-5万",
            "pay_rate_range": "70%-90%",
            "read_range": "10万以上",
        }
        for day in range(1, 13)
    ]

    assert score_account(items, fans=0).model_dump() == {
        "score": 1.5,
        "evidence": 1.0,
        "credibility": 1.5,
        "accessibility": 1.0,
        "fans": 0,
        "gmv": "1万-5万",
        "pay": "70%-90%",
        "read": "10万以上",
        "nday": 12,
        "nboard": 12,
    }
    assert score_account(items, fans=1000).accessibility == 1.0


@pytest.mark.parametrize(
    "fixture",
    json.loads(
        (Path(__file__).parent / "fixtures" / "tutorial_scoring_cases.json").read_text(
            encoding="utf-8"
        )
    ),
    ids=lambda fixture: fixture["name"],
)
def test_committed_tutorial_scoring_parity_fixtures(fixture: dict[str, object]) -> None:
    """The reviewable static fixture output must stay equal to the tutorial scorer."""
    result = score_account(fixture["items"], fans=fixture["fans"])
    assert result.model_dump() == fixture["expected"]
