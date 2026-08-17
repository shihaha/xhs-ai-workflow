from pathlib import Path

from backend.app.adapters.qianfan_playwright import QianfanPlaywrightAdapter
from backend.app.db import Database
from backend.app.features.radar.models import RankItemInput, RankSnapshotInput
from backend.app.features.radar.service import RadarService


BOARDS = ("阅读榜", "引流榜", "热卖榜", "成交榜")
DIMENSIONS = ("优秀内容", "优秀账号")


def _snapshot(
    board: str,
    dimension: str,
    *,
    raw_marker: str = "first",
    items: list[RankItemInput] | None = None,
) -> RankSnapshotInput:
    return RankSnapshotInput(
        source_date="2026-08-17",
        collected_at="2026-08-17T09:30:00+08:00",
        board=board,
        dimension=dimension,
        source_url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        raw_evidence={"capture": raw_marker, "board": board, "dimension": dimension},
        items=items
        or [
            RankItemInput(
                rank_no=1,
                title="真实原始标题",
                author_name="账号甲",
                note_id=f"{board}-{dimension}-note",
                user_id=f"{board}-{dimension}-user",
                source_url="https://ark.xiaohongshu.com/response/1",
                raw_evidence={"raw": raw_marker, "userFansNum": 321},
            )
        ],
    )


def test_eight_board_snapshots_are_unique_and_replacement_retains_new_raw_source(
    tmp_path: Path,
) -> None:
    """Reingesting one board/dimension must replace that source scope, not create a ninth snapshot."""
    service = RadarService(Database(tmp_path / "radar.sqlite3"))
    for board in BOARDS:
        for dimension in DIMENSIONS:
            service.ingest_snapshot(_snapshot(board, dimension))

    service.ingest_snapshot(_snapshot("成交榜", "优秀账号", raw_marker="replacement"))

    snapshots = service.list_snapshots(source_date="2026-08-17")
    assert len(snapshots) == 8
    replaced = next(
        row for row in snapshots if row.board == "成交榜" and row.dimension == "优秀账号"
    )
    assert replaced.raw_evidence == {
        "capture": "replacement",
        "board": "成交榜",
        "dimension": "优秀账号",
    }
    assert replaced.items[0].raw_evidence == {
        "raw": "replacement",
        "userFansNum": 321,
    }


def test_snapshot_deduplication_is_stable_and_uses_lowest_rank_evidence(
    tmp_path: Path,
) -> None:
    """Input order must not decide which duplicate note becomes scoring evidence."""
    duplicate_high = RankItemInput(
        rank_no=9,
        title="later rank",
        author_name="账号甲",
        note_id="same-note",
        user_id="same-user",
        source_url="https://ark.xiaohongshu.com/response/high",
        raw_evidence={"rank": 9},
    )
    duplicate_low = RankItemInput(
        rank_no=2,
        title="better rank",
        author_name="账号甲",
        note_id="same-note",
        user_id="same-user",
        source_url="https://ark.xiaohongshu.com/response/low",
        raw_evidence={"rank": 2},
    )
    service = RadarService(Database(tmp_path / "radar.sqlite3"))

    first = service.ingest_snapshot(
        _snapshot("成交榜", "优秀内容", items=[duplicate_high, duplicate_low])
    )
    second = service.ingest_snapshot(
        _snapshot("成交榜", "优秀内容", items=[duplicate_low, duplicate_high])
    )

    assert first.deduplicated_count == 1
    assert second.deduplicated_count == 1
    assert [item.rank_no for item in second.items] == [2]
    assert second.items[0].raw_evidence == {"rank": 2}


class _FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class _FakeResponse:
    def __init__(self, url: str, status: int, body: dict[str, object]) -> None:
        self.url = url
        self.status = status
        self._body = body

    def json(self) -> dict[str, object]:
        return self._body


class _FakePage:
    def __init__(
        self,
        *,
        url: str,
        html: str,
        selectors: dict[str, int],
        responses: list[_FakeResponse] | None = None,
    ) -> None:
        self.url = url
        self.html = html
        self.selectors = selectors
        self.responses = responses or []
        self.visited: list[str] = []
        self._response_handler: object | None = None

    def on(self, event: str, handler: object) -> None:
        if event == "response":
            self._response_handler = handler

    def goto(self, url: str, *, wait_until: str) -> None:
        self.visited.append(url)
        if callable(self._response_handler):
            for response in self.responses:
                self._response_handler(response)

    def content(self) -> str:
        return self.html

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self.selectors.get(selector, 0))


def test_controlled_adapter_returns_needs_human_with_raw_page_on_login() -> None:
    """A login wall must never be reported as a successful or empty live collection."""
    page = _FakePage(
        url="https://ark.xiaohongshu.com/login",
        html="<html><body>登录</body></html>",
        selectors={"input[type='password']": 1},
    )

    outcome = QianfanPlaywrightAdapter(page_factory=lambda: page).capture_visible_page()

    assert outcome.status == "needs_human"
    assert outcome.reason == "login_required"
    assert outcome.raw_evidence["page_html"] == "<html><body>登录</body></html>"
    assert outcome.raw_evidence["page_url"] == "https://ark.xiaohongshu.com/login"


def test_controlled_adapter_returns_needs_human_when_expected_layout_is_absent() -> None:
    """A changed page layout must stop collection rather than invent parsed ranking rows."""
    page = _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<main>unexpected layout</main>",
        selectors={"input[type='password']": 0, ".note-rank table": 0},
    )

    outcome = QianfanPlaywrightAdapter(page_factory=lambda: page).capture_visible_page()

    assert outcome.status == "needs_human"
    assert outcome.reason == "layout_changed"
    assert outcome.raw_evidence["page_html"] == "<main>unexpected layout</main>"


def test_controlled_adapter_captures_raw_page_and_response_without_writes() -> None:
    """Dropping either page or network evidence would make a successful capture unauditable."""
    response_body = {"data": {"dataList": [{"rank": 1, "userId": "account-a"}]}}
    page = _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<section class='note-rank'><table></table></section>",
        selectors={"input[type='password']": 0, ".note-rank table": 1},
        responses=[
            _FakeResponse(
                "https://ark.xiaohongshu.com/api/edith/business/data/note/rank/v2/list",
                200,
                response_body,
            )
        ],
    )

    outcome = QianfanPlaywrightAdapter(page_factory=lambda: page).capture_visible_page()

    assert outcome.status == "captured"
    assert outcome.reason is None
    assert page.visited == [
        "https://ark.xiaohongshu.com/app-datacenter/market/note-rank"
    ]
    assert outcome.raw_evidence == {
        "page_url": "https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        "page_html": "<section class='note-rank'><table></table></section>",
        "responses": [
            {
                "url": "https://ark.xiaohongshu.com/api/edith/business/data/note/rank/v2/list",
                "status": 200,
                "body": response_body,
            }
        ],
    }
