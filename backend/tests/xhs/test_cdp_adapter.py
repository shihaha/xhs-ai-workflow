from __future__ import annotations

from backend.app.adapters.contracts import CollectionRequest
from backend.app.adapters.xhs_cdp_read import XhsCdpReadAdapter


ACCOUNT = "account-a"
PROFILE_URL = f"https://www.xiaohongshu.com/user/profile/{ACCOUNT}"


def _note(index: int, *, owner: str = ACCOUNT) -> dict[str, object]:
    return {
        "noteCard": {
            "noteId": f"note-{index}",
            "displayTitle": f"Public note {index}",
            "user": {"userId": owner, "nickname": "Public author"},
            "interactInfo": {
                "likedCount": str(index),
                "collectedCount": "0",
                "commentCount": "0",
            },
        }
    }


def _request() -> CollectionRequest:
    return CollectionRequest(
        capability="fetch_account",
        parameters={"user_id": ACCOUNT, "sample_scope": "latest"},
        expected_count=None,
    )


def _snapshot(
    *,
    notes: list[dict[str, object]],
    final_url: str = PROFILE_URL,
    natural_end: bool = False,
) -> dict[str, object]:
    return {
        "final_url": final_url,
        "profile": {
            "nickname": "Public profile",
            "bio": "Public biography",
            "followers_count": 12,
        },
        "notes": notes,
        "natural_end": natural_end,
    }


def test_cdp_account_read_persists_only_latest_ten_with_explicit_identity_basis() -> None:
    """Keeping the full page payload would leak note 11 into the bounded evidence artifact."""
    adapter = XhsCdpReadAdapter(
        endpoint="http://127.0.0.1:9223",
        timeout_seconds=30,
        reader=lambda *_: _snapshot(notes=[_note(index) for index in range(12)]),
    )

    result = adapter.fetch_account(_request())

    assert result.status == "partial"
    assert result.detail == "expected_count_unknown"
    assert result.complete is False
    assert [item.id for item in result.items if item.kind == "note"] == [
        f"note:note-{index}" for index in range(10)
    ]
    assert result.raw_evidence["latest_sample"] == {
        "sample_limit": 10,
        "available_count_observed": 12,
        "completeness": "bounded_sample",
    }
    profile = next(item for item in result.items if item.kind == "profile")
    assert profile.id == f"profile:{ACCOUNT}"
    assert profile.raw_evidence["identity_basis"] == (
        "requested_profile_route_plus_consistent_note_owners"
    )
    assert profile.raw_evidence["requested_account_user_id"] == ACCOUNT
    encoded = result.model_dump_json()
    assert "note-10" not in encoded
    assert "note-11" not in encoded


def test_cdp_account_read_marks_a_proven_short_public_page_sample_exhausted() -> None:
    """A real natural end below ten is a valid sample, not a fabricated missing-note error."""
    adapter = XhsCdpReadAdapter(
        endpoint="http://127.0.0.1:9223",
        timeout_seconds=30,
        reader=lambda *_: _snapshot(
            notes=[_note(index) for index in range(6)], natural_end=True
        ),
    )

    result = adapter.fetch_account(_request())

    assert result.status == "partial"
    assert len([item for item in result.items if item.kind == "note"]) == 6
    assert result.raw_evidence["latest_sample"]["completeness"] == "sample_exhausted"


def test_cdp_account_read_rejects_an_unproven_short_sample() -> None:
    """Seeing fewer than ten without an end marker must never be treated as exhausted."""
    adapter = XhsCdpReadAdapter(
        endpoint="http://127.0.0.1:9223",
        timeout_seconds=30,
        reader=lambda *_: _snapshot(notes=[_note(index) for index in range(6)]),
    )

    result = adapter.fetch_account(_request())

    assert result.status == "needs_human"
    assert result.detail == "bounded_sample_incomplete"
    assert result.items == []


def test_cdp_account_read_rejects_route_or_note_owner_mismatch() -> None:
    """A requested route cannot authorize facts owned by another public account."""
    snapshots = (
        _snapshot(
            final_url="https://www.xiaohongshu.com/user/profile/other-account",
            notes=[_note(index) for index in range(10)],
        ),
        _snapshot(
            notes=[_note(index, owner="other-account" if index == 4 else ACCOUNT) for index in range(10)]
        ),
    )
    for snapshot in snapshots:
        adapter = XhsCdpReadAdapter(
            endpoint="http://127.0.0.1:9223",
            timeout_seconds=30,
            reader=lambda *_, value=snapshot: value,
        )

        result = adapter.fetch_account(_request())

        assert result.status == "needs_human"
        assert result.items == []


def test_cdp_account_read_reports_an_unavailable_local_browser_without_fallback() -> None:
    """A missing collection Chrome must not silently invoke the CLI or QR login."""
    def unavailable(*_: object) -> dict[str, object]:
        raise ConnectionError("local CDP is unavailable")

    adapter = XhsCdpReadAdapter(
        endpoint="http://127.0.0.1:9223",
        timeout_seconds=30,
        reader=unavailable,
    )

    result = adapter.fetch_account(_request())

    assert result.status == "needs_human"
    assert result.detail == "cdp_unavailable"
    assert result.items == []
