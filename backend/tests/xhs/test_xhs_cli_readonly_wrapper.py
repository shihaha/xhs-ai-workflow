"""Focused behavior tests for the pinned xhs-cli read-only wrapper."""

from __future__ import annotations

from types import SimpleNamespace

import backend.app.adapters.xhs_cli_readonly_wrapper as wrapper


def _note(note_id: int) -> dict[str, object]:
    return {"id": f"note-{note_id}", "noteCard": {"displayTitle": str(note_id)}}


class _FakeMouse:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    def wheel(self, horizontal: int, vertical: int) -> None:
        assert (horizontal, vertical) == (0, 1200)
        self._page.scrolls += 1
        self._page.snapshot_index = min(
            self._page.snapshot_index + 1, len(self._page.snapshots) - 1
        )


class _FakePage:
    def __init__(self, snapshots: list[list[object]]) -> None:
        self.snapshots = snapshots
        self.snapshot_index = 0
        self.scrolls = 0
        self.waits: list[int] = []
        self.mouse = _FakeMouse(self)

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)

    def evaluate(self, _fixed_snapshot_script: str) -> list[object]:
        return self.snapshots[self.snapshot_index]


def _bounded_user_posts_client(page: _FakePage) -> object:
    class Client:
        def __init__(self) -> None:
            self._page = page

        def get_user_posts(self, _user_id: str) -> list[object]:
            return page.snapshots[0]

    client_module = SimpleNamespace(XhsClient=Client)
    wrapper._install_readonly_boundary(
        SimpleNamespace(),
        SimpleNamespace(),
        {"a1": "prepared-a1", "web_session": "prepared-session"},
        client_module=client_module,
        command=["user-posts", "user-1", "--json"],
    )
    return client_module.XhsClient()


def test_user_posts_scrolls_fixed_page_slots_to_the_largest_observed_collection() -> None:
    """Returning after the first populated slot would lose later loaded public notes."""
    first_page = [_note(note_id) for note_id in range(1, 33)]
    complete_page = [
        first_page,
        [_note(32), *[_note(note_id) for note_id in range(33, 63)]],
        [],
        [],
        [],
    ]
    page = _FakePage([[first_page, [], [], [], []], complete_page, complete_page])

    rows = _bounded_user_posts_client(page).get_user_posts("user-1")

    assert [row["id"] for row in rows] == [f"note-{note_id}" for note_id in range(1, 63)]
    assert page.scrolls == 5
    assert page.waits == [1000, 1000, 1000, 1000, 1000]


def test_user_posts_keeps_the_real_partial_collection_when_scroll_adds_nothing() -> None:
    """Filling a 32-row partial result would fabricate public evidence."""
    partial_page = [[_note(note_id) for note_id in range(1, 33)], [], [], [], []]
    page = _FakePage([partial_page, partial_page])

    rows = _bounded_user_posts_client(page).get_user_posts("user-1")

    assert [row["id"] for row in rows] == [f"note-{note_id}" for note_id in range(1, 33)]
    assert page.scrolls == 5
    assert page.waits == [1000, 1000, 1000, 1000, 1000]


def test_user_posts_keeps_scrolling_when_the_first_follow_up_has_not_loaded_new_slots() -> None:
    """The first fixed wait can be too early even though the next read-only scroll loads notes."""
    first_page = [_note(note_id) for note_id in range(1, 33)]
    complete_page = [
        first_page,
        [_note(note_id) for note_id in range(33, 63)],
        [],
        [],
        [],
    ]
    page = _FakePage([
        [first_page, [], [], [], []],
        [first_page, [], [], [], []],
        complete_page,
        complete_page,
    ])

    rows = _bounded_user_posts_client(page).get_user_posts("user-1")

    assert [row["id"] for row in rows] == [f"note-{note_id}" for note_id in range(1, 63)]
    assert page.scrolls == 5
    assert page.waits == [1000, 1000, 1000, 1000, 1000]


def test_user_posts_uses_all_five_fixed_scrolls_when_two_initial_waits_have_no_growth() -> None:
    """Stopping before the fixed fifth read can still lose the final lazy-loaded slot."""
    first_page = [_note(note_id) for note_id in range(1, 33)]
    page = _FakePage([
        [first_page, [], [], [], []],
        [first_page, [], [], [], []],
        [first_page, [], [], [], []],
        [first_page, [_note(note_id) for note_id in range(33, 63)], [], [], []],
        [first_page, [_note(note_id) for note_id in range(33, 93)], [], [], []],
        [first_page, [_note(note_id) for note_id in range(33, 123)], [], [], []],
    ])

    rows = _bounded_user_posts_client(page).get_user_posts("user-1")

    assert [row["id"] for row in rows] == [f"note-{note_id}" for note_id in range(1, 123)]
    assert page.scrolls == 5
    assert page.waits == [1000, 1000, 1000, 1000, 1000]


def test_user_posts_deduplicates_repeated_note_ids_without_reordering_first_observations() -> None:
    """Repeated page slots must not inflate counts or reorder the first public observation."""
    initial = [[_note(2), _note(1)], [], [], [], []]
    later = [[_note(1), _note(3), _note(2)], [], [], [], []]
    page = _FakePage([initial, later, later])

    rows = _bounded_user_posts_client(page).get_user_posts("user-1")

    assert [row["id"] for row in rows] == ["note-2", "note-1", "note-3"]


def test_non_user_posts_commands_keep_the_verified_client_method_unchanged() -> None:
    """Installing the supplement on another allowlisted command would change its read contract."""
    class Client:
        def get_user_posts(self, user_id: str) -> list[dict[str, str]]:
            return [{"id": user_id}]

    client_module = SimpleNamespace(XhsClient=Client)
    wrapper._install_readonly_boundary(
        SimpleNamespace(),
        SimpleNamespace(),
        {"a1": "prepared-a1", "web_session": "prepared-session"},
        client_module=client_module,
        command=["user", "user-1", "--json"],
    )

    assert client_module.XhsClient().get_user_posts("user-1") == [{"id": "user-1"}]
