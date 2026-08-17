import pytest
from pydantic import ValidationError

from backend.app.adapters.contracts import CollectionItem, CollectionResult


def _item(item_id: str) -> CollectionItem:
    return CollectionItem(
        id=item_id,
        kind="note",
        source_url="https://www.xiaohongshu.com/explore/example",
        raw_evidence={"response": {"id": item_id}},
    )


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
            expected_count=2,
            succeeded_count=1,
            missing_items=[],
            complete=True,
        )


def test_collection_result_requires_each_missing_item_to_account_for_partial_nn() -> None:
    """Dropping a missing-item record would hide why a collection is only 1/2."""
    with pytest.raises(ValidationError):
        CollectionResult(
            items=[_item("note-1")],
            expected_count=2,
            succeeded_count=1,
            missing_items=[],
            complete=False,
        )


def test_collection_result_rejects_duplicate_success_ids_that_falsely_claim_nn() -> None:
    """Counting the same item twice would falsely represent one saved item as a 2/2 collection."""
    with pytest.raises(ValidationError):
        CollectionResult(
            items=[_item("note-1"), _item("note-1")],
            expected_count=2,
            succeeded_count=2,
            missing_items=[],
            complete=True,
        )
