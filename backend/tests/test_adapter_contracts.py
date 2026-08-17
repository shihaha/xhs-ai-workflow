import pytest
from pydantic import ValidationError

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionResult,
    MissingCollectionItem,
)


def _item(item_id: str) -> CollectionItem:
    return CollectionItem(
        id=item_id,
        kind="note",
        source_url="https://www.xiaohongshu.com/explore/example",
        raw_evidence={"response": {"id": item_id}},
    )


def _missing(reference: str) -> MissingCollectionItem:
    return MissingCollectionItem(
        reference=reference,
        reason="not_observed",
        raw_evidence={"reference": reference},
    )


def _rejected(reference: str) -> dict[str, object]:
    return {
        "reference": reference,
        "reason": "identity_insufficient",
        "raw_evidence": {"row": reference},
    }


def test_collection_result_preserves_rejected_observations_separately_from_deficit() -> None:
    """A rejected observed row must not erase an accepted row or masquerade as missing."""
    result = CollectionResult(
        status="needs_human",
        detail="response_unusable",
        items=[_item("note-1")],
        rejected_items=[_rejected("row-2")],
        expected_count_known=True,
        expected_count=2,
        succeeded_count=1,
        observed_count=2,
        missing_items=[],
        overflow_count=0,
        complete=False,
    )

    assert result.items == [_item("note-1")]
    assert result.rejected_items[0].reference == "row-2"
    assert result.observed_count == 2
    assert result.missing_items == []


def test_collection_result_rejects_incorrect_observed_count() -> None:
    """Published observation accounting must equal accepted plus rejected rows."""
    with pytest.raises(ValidationError):
        CollectionResult(
            status="succeeded",
            items=[_item("note-1")],
            rejected_items=[],
            expected_count_known=True,
            expected_count=1,
            succeeded_count=1,
            observed_count=2,
            missing_items=[],
            overflow_count=0,
            complete=True,
        )


def test_collection_result_rejects_success_when_any_observation_was_rejected() -> None:
    """An exact accepted count cannot hide a separately rejected source row."""
    with pytest.raises(ValidationError):
        CollectionResult(
            status="succeeded",
            items=[_item("note-1")],
            rejected_items=[_rejected("row-2")],
            expected_count_known=True,
            expected_count=2,
            succeeded_count=1,
            observed_count=2,
            missing_items=[],
            overflow_count=0,
            complete=True,
        )


def test_collection_result_infers_duplicate_url_observations_from_rejection_reason() -> None:
    """A duplicate URL row must not become overflow because a producer omitted a count."""
    result = CollectionResult(
        status="succeeded",
        items=[_item("note-1")],
        rejected_items=[
            {
                "reference": "shop-card-2",
                "reason": "duplicate_source_url",
                "raw_evidence": {
                    "source_url": "https://www.xiaohongshu.com/explore/example"
                },
            }
        ],
        expected_count_known=True,
        expected_count=1,
        succeeded_count=1,
        observed_count=2,
        missing_items=[],
        overflow_count=0,
        complete=True,
    )

    assert result.raw_observation_count == 2
    assert result.duplicate_observation_count == 1
    assert result.overflow_count == 0
    assert result.complete is True


@pytest.mark.parametrize(
    ("source_url", "raw_evidence"),
    [
        (None, {"response": {"id": "note-1"}}),
        ("https://www.xiaohongshu.com/explore/example", {}),
    ],
)
def test_collection_item_rejects_missing_source_or_raw_evidence(
    source_url: str | None, raw_evidence: dict[str, object]
) -> None:
    """Removing either evidence boundary would permit an untraceable collection item."""
    with pytest.raises(ValidationError):
        CollectionItem(
            id="note-1",
            kind="note",
            source_url=source_url,
            raw_evidence=raw_evidence,
        )


def test_collection_result_rejects_claimed_nn_completion_with_missing_successes() -> None:
    """Treating one successful item as 2/2 would falsely close an incomplete collection."""
    with pytest.raises(ValidationError):
        CollectionResult(
            items=[_item("note-1")],
            expected_count_known=True,
            expected_count=2,
            succeeded_count=1,
            missing_items=[],
            overflow_count=0,
            complete=True,
        )


def test_collection_result_requires_each_missing_item_to_account_for_partial_nn() -> None:
    """Dropping a missing-item record would hide why a collection is only 1/2."""
    with pytest.raises(ValidationError):
        CollectionResult(
            items=[_item("note-1")],
            expected_count_known=True,
            expected_count=2,
            succeeded_count=1,
            missing_items=[],
            overflow_count=0,
            complete=False,
        )


def test_collection_result_rejects_duplicate_success_ids_that_falsely_claim_nn() -> None:
    """Counting the same item twice would falsely represent one saved item as a 2/2 collection."""
    with pytest.raises(ValidationError):
        CollectionResult(
            items=[_item("note-1"), _item("note-1")],
            expected_count_known=True,
            expected_count=2,
            succeeded_count=2,
            missing_items=[],
            overflow_count=0,
            complete=True,
        )


@pytest.mark.parametrize("result_status", ["partial", "failed", "needs_human"])
def test_collection_result_rejects_complete_non_success_status(
    result_status: str,
) -> None:
    """No non-success status may simultaneously claim an exact completed result."""
    with pytest.raises(ValidationError):
        CollectionResult(
            status=result_status,
            items=[_item("note-1")],
            expected_count_known=True,
            expected_count=1,
            succeeded_count=1,
            overflow_count=0,
            complete=True,
        )


def test_collection_result_rejects_incomplete_succeeded_status() -> None:
    """A succeeded label must not hide unknown or missing expected results."""
    with pytest.raises(ValidationError):
        CollectionResult(
            status="succeeded",
            items=[_item("note-1")],
            expected_count_known=False,
            expected_count=None,
            succeeded_count=1,
            overflow_count=0,
            complete=False,
        )


def test_failed_zero_expected_result_can_remain_incomplete() -> None:
    """Exact normalized counts do not erase a separately observed collection contradiction."""
    result = CollectionResult(
        status="failed",
        detail="observed_count_exceeds_expected",
        items=[],
        expected_count_known=True,
        expected_count=0,
        succeeded_count=0,
        missing_items=[],
        overflow_count=0,
        complete=False,
    )

    assert result.status == "failed"
    assert result.complete is False


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "succeeded",
            "items": [_item("note-1")],
            "expected_count_known": True,
            "expected_count": 1,
            "succeeded_count": 1,
            "missing_items": [],
            "overflow_count": 0,
            "complete": True,
        },
        {
            "status": "partial",
            "items": [_item("note-1")],
            "expected_count_known": True,
            "expected_count": 2,
            "succeeded_count": 1,
            "missing_items": [_missing("note-2")],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "needs_human",
            "items": [],
            "expected_count_known": True,
            "expected_count": 2,
            "succeeded_count": 0,
            "missing_items": [_missing("note-1"), _missing("note-2")],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "failed",
            "items": [],
            "expected_count_known": True,
            "expected_count": 2,
            "succeeded_count": 0,
            "missing_items": [_missing("note-1"), _missing("note-2")],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "failed",
            "items": [],
            "expected_count_known": True,
            "expected_count": 0,
            "succeeded_count": 0,
            "missing_items": [],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "failed",
            "items": [_item("note-1"), _item("note-2")],
            "expected_count_known": True,
            "expected_count": 1,
            "succeeded_count": 2,
            "missing_items": [],
            "overflow_count": 1,
            "complete": False,
        },
        {
            "status": "partial",
            "items": [_item("note-1")],
            "expected_count_known": False,
            "expected_count": None,
            "succeeded_count": 1,
            "missing_items": [],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "needs_human",
            "items": [],
            "expected_count_known": False,
            "expected_count": None,
            "succeeded_count": 0,
            "missing_items": [],
            "overflow_count": 0,
            "complete": False,
        },
    ],
    ids=[
        "known-complete",
        "known-partial-deficit",
        "known-needs-human-deficit",
        "known-failed-deficit",
        "known-failed-zero-of-zero",
        "known-failed-overflow",
        "unknown-partial-observations",
        "unknown-needs-human",
    ],
)
def test_collection_result_accepts_only_truthfully_accounted_states(
    payload: dict[str, object],
) -> None:
    """Every supported known/unknown outcome has literal, auditable accounting."""
    result = CollectionResult(**payload)

    assert result.succeeded_count == len(result.items)
    assert result.overflow_count == payload["overflow_count"]
    assert result.expected_count_known is payload["expected_count_known"]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "partial",
            "items": [_item("note-1")],
            "expected_count_known": True,
            "expected_count": 1,
            "succeeded_count": 1,
            "missing_items": [],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "failed",
            "items": [_item("note-1")],
            "expected_count_known": True,
            "expected_count": 1,
            "succeeded_count": 1,
            "missing_items": [],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "needs_human",
            "items": [_item("note-1")],
            "expected_count_known": True,
            "expected_count": 1,
            "succeeded_count": 1,
            "missing_items": [],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "partial",
            "items": [],
            "expected_count_known": True,
            "expected_count": 0,
            "succeeded_count": 0,
            "missing_items": [],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "partial",
            "items": [_item("note-1")],
            "expected_count_known": True,
            "expected_count": None,
            "succeeded_count": 1,
            "missing_items": [],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "succeeded",
            "items": [_item("note-1")],
            "expected_count_known": False,
            "expected_count": 1,
            "succeeded_count": 1,
            "missing_items": [],
            "overflow_count": 0,
            "complete": True,
        },
        {
            "status": "partial",
            "items": [_item("note-1")],
            "expected_count_known": False,
            "expected_count": None,
            "succeeded_count": 1,
            "missing_items": [_missing("unknown-note")],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "failed",
            "items": [_item("note-1"), _item("note-2")],
            "expected_count_known": True,
            "expected_count": 1,
            "succeeded_count": 2,
            "missing_items": [],
            "overflow_count": 0,
            "complete": False,
        },
        {
            "status": "partial",
            "items": [_item("note-1"), _item("note-2")],
            "expected_count_known": True,
            "expected_count": 1,
            "succeeded_count": 2,
            "missing_items": [],
            "overflow_count": 1,
            "complete": False,
        },
    ],
    ids=[
        "partial-without-deficit",
        "failed-positive-exact",
        "needs-human-positive-exact",
        "partial-zero-of-zero",
        "known-without-total",
        "unknown-with-total",
        "unknown-with-fabricated-missing",
        "unaccounted-overflow",
        "overflow-not-failed",
    ],
)
def test_collection_result_rejects_contradictory_accounting(
    payload: dict[str, object],
) -> None:
    """A contradictory status, known-total flag, deficit, or overflow must fail validation."""
    with pytest.raises(ValidationError):
        CollectionResult(**payload)
