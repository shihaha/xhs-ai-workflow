"""Read bounded public account samples from a trusted local Chrome CDP endpoint."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import ValidationError

from backend.app.adapters.contracts import CollectionItem, CollectionRequest, CollectionResult
from backend.app.adapters.xhs_cli_read import (
    XhsCliAccountRequest,
    _accounted_result,
    _normalize_note_rows,
    _profile_public_data,
    _response_ref,
)


_PUBLIC_ORIGIN = "https://www.xiaohongshu.com"
_IDENTITY_BASIS = "requested_profile_route_plus_consistent_note_owners"
_SAMPLE_LIMIT = 10
SnapshotReader = Callable[[str, str, float], dict[str, Any]]


class XhsCdpReadAdapter:
    """Explicit account-only adapter selected through trusted Settings."""

    capabilities = frozenset({"fetch_account"})

    def __init__(
        self,
        *,
        endpoint: str,
        timeout_seconds: float,
        reader: SnapshotReader | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        self._reader = reader or _read_public_account_snapshot
        self._lock = Lock()
        self._closed = False
        self._active = 0

    @classmethod
    def from_settings(cls, settings: Any) -> "XhsCdpReadAdapter":
        return cls(
            endpoint=settings.xhs_cdp_endpoint,
            timeout_seconds=settings.xhs_cdp_timeout_seconds,
        )

    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        try:
            parsed = XhsCliAccountRequest.model_validate(request.parameters)
        except ValidationError:
            return _needs_human(request, "invalid_request")
        if request.capability != "fetch_account" or parsed.sample_scope != "latest":
            return _needs_human(request, "invalid_request")

        with self._lock:
            if self._closed:
                return _needs_human(request, "cdp_unavailable")
            self._active += 1
        try:
            snapshot = self._reader(
                self._endpoint, parsed.user_id, self._timeout_seconds
            )
        except Exception:
            return _needs_human(request, "cdp_unavailable")
        finally:
            with self._lock:
                self._active -= 1

        if not isinstance(snapshot, dict) or not _route_matches(
            snapshot.get("final_url"), parsed.user_id
        ):
            return _needs_human(request, "profile_identity_mismatch")
        notes = snapshot.get("notes")
        profile = snapshot.get("profile")
        if not isinstance(notes, list) or not isinstance(profile, dict):
            return _needs_human(request, "response_unusable")

        unique_rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for row in notes:
            note_id = _note_identity(row)
            if not isinstance(row, dict) or note_id is None or note_id in seen_ids:
                continue
            seen_ids.add(note_id)
            unique_rows.append(row)
        available_count = len(unique_rows)
        selected_rows = unique_rows[:_SAMPLE_LIMIT]
        if len(selected_rows) < _SAMPLE_LIMIT and snapshot.get("natural_end") is not True:
            return _needs_human(request, "bounded_sample_incomplete")

        response_ref = _response_ref("cdp-user-posts", selected_rows)
        note_items, rejected = _normalize_note_rows(
            selected_rows,
            source="cdp-user-posts",
            response_ref=response_ref,
        )
        by_id = {item.id.removeprefix("note:"): item for item in note_items}
        ordered_items = [
            by_id[note_id]
            for note_id in map(_note_identity, selected_rows)
            if note_id in by_id
        ]
        if rejected or len(ordered_items) != len(selected_rows):
            return _needs_human(request, "response_unusable")
        if any(item.data.get("user_id") != parsed.user_id for item in ordered_items):
            return _needs_human(request, "note_owner_mismatch")

        final_url = str(snapshot["final_url"])
        profile_item = CollectionItem(
            id=f"profile:{parsed.user_id}",
            kind="profile",
            source_url=final_url,
            raw_evidence={
                "identity_basis": _IDENTITY_BASIS,
                "requested_account_user_id": parsed.user_id,
                "final_profile_url": final_url,
                "consistent_note_owner_count": len(ordered_items),
                "public_profile": profile,
            },
            data=_profile_public_data(profile, user_id=parsed.user_id),
        )
        completeness = (
            "bounded_sample" if len(selected_rows) == _SAMPLE_LIMIT else "sample_exhausted"
        )
        return _accounted_result(
            request=request,
            items=[profile_item, *ordered_items],
            rejected_items=[],
            raw_evidence={
                "provider": "chrome_cdp",
                "identity_basis": _IDENTITY_BASIS,
                "profile": profile_item.raw_evidence,
                "notes": selected_rows,
                "response_ref": response_ref,
                "latest_sample": {
                    "sample_limit": _SAMPLE_LIMIT,
                    "available_count_observed": available_count,
                    "completeness": completeness,
                },
            },
        )

    def close(self, *, timeout: float = 0.25) -> bool:
        del timeout
        with self._lock:
            self._closed = True
            return self._active == 0


def _needs_human(request: CollectionRequest, detail: str) -> CollectionResult:
    return CollectionResult(
        status="needs_human",
        detail=detail,
        raw_evidence={"provider": "chrome_cdp", "category": detail},
        items=[],
        expected_count_known=False,
        expected_count=None,
        succeeded_count=0,
        observed_count=0,
        missing_items=[],
        overflow_count=0,
        complete=False,
    )


def _route_matches(value: Any, account_user_id: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError, UnicodeError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.xiaohongshu.com"
        and parsed.path.rstrip("/")
        == f"/user/profile/{quote(account_user_id, safe='')}"
    )


def _note_identity(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    card = row.get("noteCard") if isinstance(row.get("noteCard"), dict) else row
    for key in ("noteId", "note_id", "id"):
        value = card.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _read_public_account_snapshot(
    endpoint: str, account_user_id: str, timeout_seconds: float
) -> dict[str, Any]:
    """Connect to an already-running visible Chrome and return public page facts only."""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = None
    page = None
    try:
        browser = playwright.chromium.connect_over_cdp(
            endpoint, timeout=int(timeout_seconds * 1000)
        )
        if not browser.contexts:
            raise ConnectionError("CDP browser has no context")
        page = browser.contexts[0].new_page()
        page.goto(
            f"{_PUBLIC_ORIGIN}/user/profile/{quote(account_user_id, safe='')}",
            wait_until="domcontentloaded",
            timeout=int(timeout_seconds * 1000),
        )
        page.wait_for_function(
            "() => Boolean(window.__INITIAL_STATE__?.user?.notes)",
            timeout=int(timeout_seconds * 1000),
        )
        snapshot = page.evaluate(
            """() => {
              const state = window.__INITIAL_STATE__ || {};
              const user = state.user || {};
              const profile = user.userPageData?.basicInfo || user.userInfo || {};
              const slots = Array.isArray(user.notes) ? user.notes : Object.values(user.notes || {});
              const rows = [];
              const visit = (value) => {
                if (Array.isArray(value)) { value.forEach(visit); return; }
                if (!value || typeof value !== 'object') return;
                if (value.noteCard || value.noteId || value.note_id) { rows.push(value); return; }
                for (const child of Object.values(value)) visit(child);
              };
              slots.forEach(visit);
              const slimNotes = rows.map((row) => {
                const card = row.noteCard || row;
                const owner = card.user || row.user || {};
                const interact = card.interactInfo || card.interact_info || {};
                return {noteCard: {
                  noteId: card.noteId || card.note_id || card.id,
                  displayTitle: card.displayTitle || card.title || '',
                  desc: card.desc || card.description || '',
                  type: card.type || '',
                  cover: card.cover || {},
                  user: {userId: owner.userId || owner.user_id || owner.id, nickname: owner.nickname || ''},
                  interactInfo: {
                    likedCount: interact.likedCount || interact.liked_count || '0',
                    collectedCount: interact.collectedCount || interact.collected_count || '0',
                    commentCount: interact.commentCount || interact.comment_count || '0'
                  }
                }};
              });
              const text = document.body?.innerText || '';
              return {
                final_url: location.href,
                profile: {
                  nickname: profile.nickname || profile.nickName || '',
                  bio: profile.desc || profile.bio || '',
                  red_id: profile.redId || profile.red_id || '',
                  followers_count: profile.followersCount || profile.followers_count || 0,
                  following_count: profile.follows || profile.following_count || 0
                },
                notes: slimNotes,
                natural_end: /没有更多|还没有发布任何内容/.test(text)
              };
            }"""
        )
        if not isinstance(snapshot, dict):
            raise ValueError("CDP page returned an unusable snapshot")
        return snapshot
    finally:
        if page is not None:
            page.close()
        if browser is not None:
            browser.close()
        playwright.stop()
