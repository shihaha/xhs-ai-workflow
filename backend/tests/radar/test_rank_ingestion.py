import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import backend.app.adapters.qianfan_playwright as qianfan_module
from backend.app.adapters.qianfan_playwright import QianfanPlaywrightAdapter
from backend.app.adapters.contracts import CollectionRequest, CollectionResult
from backend.app.adapters.registry import AdapterRegistry
from backend.app.db import Database
from backend.app.features.radar.models import RankItemInput, RankSnapshotInput
from backend.app.features.radar.service import RadarService
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobNotFound, JobService


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


def test_fallback_dedupe_keeps_distinct_notes_from_one_user(tmp_path: Path) -> None:
    """Using user_id as fallback identity must not collapse different notes by one account."""
    first = RankItemInput(
        rank_no=3,
        title="第一篇",
        author_name="账号甲",
        user_id="same-user",
        publish_date="2026-08-17 09:00",
        source_url="https://ark.xiaohongshu.com/api/rank",
        raw_evidence={"noteTitle": "第一篇", "rank": 3},
    )
    first_duplicate = first.model_copy(
        update={
            "rank_no": 8,
            "raw_evidence": {"noteTitle": "第一篇", "rank": 8, "read": "10万以上"},
        }
    )
    second = RankItemInput(
        rank_no=4,
        title="第二篇",
        author_name="账号甲",
        user_id="same-user",
        publish_date="2026-08-17 10:00",
        source_url="https://ark.xiaohongshu.com/api/rank",
        raw_evidence={"noteTitle": "第二篇", "rank": 4},
    )
    service = RadarService(Database(tmp_path / "radar.sqlite3"))

    snapshot = service.ingest_snapshot(
        _snapshot(
            "成交榜",
            "优秀内容",
            items=[first_duplicate, second, first],
        )
    )

    assert snapshot.deduplicated_count == 2
    assert [(item.rank_no, item.title) for item in snapshot.items] == [
        (3, "第一篇"),
        (4, "第二篇"),
    ]


class _FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class _FakeResponse:
    def __init__(
        self,
        url: str,
        status: int,
        body: dict[str, object] | None = None,
        *,
        raw_text: str | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self._body = body
        self._raw_text = raw_text
        self._json_error = json_error

    def json(self) -> dict[str, object]:
        if self._json_error is not None:
            raise self._json_error
        assert self._body is not None
        return self._body

    def text(self) -> str:
        if self._raw_text is None:
            raise RuntimeError("text not configured")
        return self._raw_text


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
        self.removed_listeners: list[tuple[str, object]] = []
        self.closed = False
        self.goto_error: Exception | None = None
        self.locator_error: Exception | None = None
        self.remove_listener_error: Exception | None = None
        self.close_error: Exception | None = None
        self._response_handler: object | None = None

    def on(self, event: str, handler: object) -> None:
        if event == "response":
            self._response_handler = handler

    def goto(self, url: str, *, wait_until: str) -> None:
        self.visited.append(url)
        if self.goto_error is not None:
            raise self.goto_error
        self.emit_responses()

    def emit_responses(self) -> None:
        if callable(self._response_handler):
            for response in self.responses:
                self._response_handler(response)
            self.responses.clear()

    def content(self) -> str:
        return self.html

    def locator(self, selector: str) -> _FakeLocator:
        if self.locator_error is not None:
            raise self.locator_error
        return _FakeLocator(self.selectors.get(selector, 0))

    def remove_listener(self, event: str, handler: object) -> None:
        self.removed_listeners.append((event, handler))
        if self.remove_listener_error is not None:
            raise self.remove_listener_error

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _ranking_page(items: list[dict[str, object]]) -> _FakePage:
    return _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<section class='note-rank'><table></table></section>",
        selectors={"input[type='password']": 0, ".note-rank table": 1},
        responses=[
            _FakeResponse(
                "https://ark.xiaohongshu.com/api/edith/business/data/note/rank/v2/list",
                200,
                {"data": {"dataList": items}},
            )
        ],
    )


def test_controlled_adapter_returns_needs_human_with_raw_page_on_login() -> None:
    """A login wall must never be reported as a successful or empty live collection."""
    page = _FakePage(
        url="https://ark.xiaohongshu.com/login",
        html="<html><body>登录</body></html>",
        selectors={"input[type='password']": 1},
    )

    outcome = QianfanPlaywrightAdapter(
        page_factory=lambda: page, timeout_seconds=0
    ).capture_visible_page()

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

    outcome = QianfanPlaywrightAdapter(
        page_factory=lambda: page, timeout_seconds=0
    ).capture_visible_page()

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
    assert len(page.removed_listeners) == 1


def test_malformed_response_records_only_parse_category_and_body_length() -> None:
    """A JSON parse failure remains auditable without persisting opaque response text."""
    page = _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<section class='note-rank'><table></table></section>",
        selectors={"input[type='password']": 0, ".note-rank table": 1},
        responses=[
            _FakeResponse(
                "https://ark.xiaohongshu.com/api/edith/business/data/note/rank/v2/list",
                502,
                raw_text="<html>bad gateway</html>",
                json_error=ValueError("not json"),
            )
        ],
    )

    outcome = QianfanPlaywrightAdapter(page_factory=lambda: page).capture_visible_page()

    response = outcome.raw_evidence["responses"][0]
    assert response["capture_error"] == "ValueError"
    assert response["body_length"] == len("<html>bad gateway</html>")
    assert "raw_text" not in response


def test_capture_waits_for_delayed_ranking_response_and_cleans_listener() -> None:
    """A response arriving within the bounded wait must be captured before deciding layout changed."""
    response_body = {"data": {"dataList": [{"rank": 1, "userId": "delayed"}]}}
    page = _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<main>loading</main>",
        selectors={"input[type='password']": 0, ".note-rank table": 1},
        responses=[
            _FakeResponse(
                "https://ark.xiaohongshu.com/api/edith/business/data/note/rank/v2/list",
                200,
                response_body,
            )
        ],
    )
    page.responses_to_delay = page.responses
    page.responses = []

    def release_response(_: float) -> None:
        page.responses = page.responses_to_delay
        page.emit_responses()

    outcome = QianfanPlaywrightAdapter(
        page_factory=lambda: page,
        timeout_seconds=1,
        poll_interval=0.01,
        sleep=release_response,
    ).capture_visible_page()

    assert outcome.status == "captured"
    assert outcome.raw_evidence["responses"][0]["body"] == response_body
    assert len(page.removed_listeners) == 1


def test_cleanup_failures_are_evidence_and_do_not_replace_captured_result() -> None:
    """Listener/page cleanup errors must be recorded after, not override, the capture fact."""
    response_body = {"data": {"dataList": [{"rank": 1, "noteId": "cleanup-note"}]}}
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
    page.remove_listener_error = RuntimeError("listener cleanup failed")
    page.close_error = RuntimeError("page close failed")

    outcome = QianfanPlaywrightAdapter(
        page_factory=lambda: page, owns_page=True
    ).capture_visible_page()

    assert outcome.status == "captured"
    assert outcome.raw_evidence["cleanup_errors"] == [
        {
            "stage": "listener_cleanup",
            "type": "RuntimeError",
            "message": "listener cleanup failed",
        },
        {
            "stage": "page_close",
            "type": "RuntimeError",
            "message": "page close failed",
        },
    ]


def test_navigation_failure_is_needs_human_with_evidence_and_owned_page_cleanup() -> None:
    """A browser timeout must leave factual evidence and close only a page the adapter owns."""
    page = _FakePage(
        url="about:blank",
        html="<main>not loaded</main>",
        selectors={},
    )
    page.goto_error = TimeoutError("navigation timed out")

    outcome = QianfanPlaywrightAdapter(
        page_factory=lambda: page,
        timeout_seconds=0,
        owns_page=True,
    ).capture_visible_page()

    assert outcome.status == "needs_human"
    assert outcome.reason == "navigation_failed"
    assert outcome.raw_evidence["capture_errors"] == [
        {"stage": "navigation", "type": "TimeoutError", "message": "navigation timed out"}
    ]
    assert len(page.removed_listeners) == 1
    assert page.closed is True


def test_locator_failure_is_needs_human_with_layout_error_evidence() -> None:
    """A selector API failure must be an observed layout problem, not an uncaught crash."""
    page = _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<main>unknown</main>",
        selectors={},
    )
    page.locator_error = RuntimeError("locator unavailable")

    outcome = QianfanPlaywrightAdapter(
        page_factory=lambda: page, timeout_seconds=0
    ).capture_visible_page()

    assert outcome.status == "needs_human"
    assert outcome.reason == "layout_changed"
    assert outcome.raw_evidence["capture_errors"] == [
        {"stage": "layout", "type": "RuntimeError", "message": "locator unavailable"}
    ]


def test_collect_rankings_never_claims_empty_success_without_a_ranking_response() -> None:
    """A visible table without parseable response rows cannot truthfully become 0/0 success."""
    page = _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<section class='note-rank'><table></table></section>",
        selectors={"input[type='password']": 0, ".note-rank table": 1},
    )
    adapter = QianfanPlaywrightAdapter(page_factory=lambda: page, timeout_seconds=0)

    result = adapter.collect_rankings(CollectionRequest(capability="rankings"))

    assert result.status == "needs_human"
    assert result.detail == "response_not_observed"
    assert result.expected_count_known is False
    assert result.complete is False
    assert result.missing_items == []


def test_collect_rankings_returns_contract_and_attaches_raw_job_evidence(
    tmp_path: Path,
) -> None:
    """The selectable collector boundary must return normalized items and durable raw evidence."""
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    response_body: dict[str, Any] = {
        "data": {
            "dataList": [
                {
                    "rank": 1,
                    "noteId": "note-a",
                    "userId": "account-a",
                    "userNickname": "账号甲",
                }
            ]
        }
    }
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
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: page, job_service=jobs, runtime_dir=runtime_dir
    )
    registry = AdapterRegistry()
    registry.register(
        name="qianfan-playwright",
        adapter=adapter,
        capabilities={"rankings"},
        priority=10,
    )

    selected = registry.resolve("rankings")
    result = selected.collect_rankings(
        CollectionRequest(
            capability="rankings", parameters={"job_id": job.id}, expected_count=1
        )
    )

    assert isinstance(result, CollectionResult)
    assert result.status == "succeeded"
    assert result.complete is True
    assert result.items[0].id == "note-a"
    persisted_job = jobs.get(job.id)
    assert len(persisted_job.artifacts) == 1
    artifact = persisted_job.artifacts[0]
    assert artifact.kind == "qianfan_raw_capture"
    assert (runtime_dir / artifact.path).is_file()
    assert result.evidence_artifacts == [artifact.path]
    persisted_evidence = json.loads(
        (runtime_dir / artifact.path).read_text(encoding="utf-8")
    )
    assert persisted_evidence["raw_evidence"]["responses"][0]["body"] == response_body
    assert response_body["data"]["dataList"][0] == result.items[0].raw_evidence["item"]
    assert jobs.get(job.id).state is JobState.succeeded


def test_qianfan_item_identity_ignores_mutable_rank_and_metric_evidence() -> None:
    """Changing only rank/read/GMV/pay/raw metrics must not create a new semantic note ID."""
    first_page = _ranking_page(
        [
            {
                "rank": 1,
                "userId": "same-account",
                "noteTitle": "同一篇笔记",
                "publishTime": "2026-08-17 09:00",
                "contentType": "image",
                "userNickname": "同一作者",
                "readRange": "1万-5万",
                "gmvRange": "100-200",
                "payRange": "10-20",
                "rawMetricEvidence": {"rank": 1, "read": 12000},
            }
        ]
    )
    changed_metrics_page = _ranking_page(
        [
            {
                "rank": 9,
                "userId": "same-account",
                "noteTitle": "同一篇笔记",
                "publishTime": "2026-08-17 09:00",
                "contentType": "image",
                "userNickname": "同一作者",
                "readRange": "10万以上",
                "gmvRange": "1000以上",
                "payRange": "100以上",
                "rawMetricEvidence": {"rank": 9, "read": 180000},
            }
        ]
    )

    first = QianfanPlaywrightAdapter(page_factory=lambda: first_page).collect_rankings(
        CollectionRequest(capability="rankings", expected_count=1)
    )
    changed = QianfanPlaywrightAdapter(
        page_factory=lambda: changed_metrics_page
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=1))

    assert first.items[0].id == changed.items[0].id
    assert first.items[0].raw_evidence != changed.items[0].raw_evidence


def test_qianfan_item_identity_ignores_nickname_changes() -> None:
    """A mutable account nickname must not change one note's fallback identity."""
    before = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page(
            [
                {
                    "rank": 1,
                    "userId": "stable-account",
                    "noteTitle": "同一篇笔记",
                    "publishTime": "2026-08-17 09:00",
                    "contentType": "image",
                    "userNickname": "旧昵称",
                }
            ]
        )
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=1))
    after = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page(
            [
                {
                    "rank": 7,
                    "userId": "stable-account",
                    "noteTitle": "同一篇笔记",
                    "publishTime": "2026-08-17 09:00",
                    "contentType": "image",
                    "userNickname": "新昵称",
                }
            ]
        )
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=1))

    assert before.status == "succeeded"
    assert after.status == "succeeded"
    assert before.items[0].id == after.items[0].id


def test_identity_insufficient_rows_are_explicit_rejected_evidence_not_deduped() -> None:
    """Multiple all-null semantic rows must remain rejected observations, never missing."""
    raw_rows = [{"rank": 1}, {"rank": 2}]
    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page(raw_rows)
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=2))

    assert result.status == "needs_human"
    assert result.detail == "response_unusable"
    assert result.succeeded_count == 0
    assert result.observed_count == 2
    assert result.items == []
    assert result.missing_items == []
    assert [rejected.reason for rejected in result.rejected_items] == [
        "identity_insufficient",
        "identity_insufficient",
    ]
    assert [rejected.raw_evidence["item"] for rejected in result.rejected_items] == raw_rows
    assert result.complete is False


def test_mixed_accepted_and_rejected_rows_preserve_exact_known_accounting() -> None:
    """One accepted plus one rejected observation is 2 observed, not 1 accepted plus 1 missing."""
    raw_rejected = {
        "rank": 2,
        "userId": "account-b",
        "noteTitle": "缺发布时间",
        "contentType": "image",
    }
    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page(
            [{"rank": 1, "noteId": "accepted-note"}, raw_rejected]
        )
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=2))

    assert result.status == "needs_human"
    assert result.detail == "response_unusable"
    assert [item.id for item in result.items] == ["accepted-note"]
    assert result.succeeded_count == 1
    assert result.observed_count == 2
    assert result.missing_items == []
    assert result.overflow_count == 0
    assert result.rejected_items[0].reference == "qianfan_response:1:item:2"
    assert result.rejected_items[0].reason == "identity_insufficient"
    assert result.rejected_items[0].raw_evidence["item"] == raw_rejected
    assert result.complete is False


def test_mixed_accepted_and_rejected_rows_count_overflow_from_all_observations() -> None:
    """One accepted plus one rejected observation over expected one must report one overflow."""
    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page(
            [
                {"rank": 1, "noteId": "accepted-note"},
                {"rank": 2, "userId": "account-b", "noteTitle": "无发布时间"},
            ]
        )
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=1))

    assert result.status == "failed"
    assert result.detail == "observed_count_exceeds_expected"
    assert [item.id for item in result.items] == ["accepted-note"]
    assert len(result.rejected_items) == 1
    assert result.observed_count == 2
    assert result.missing_items == []
    assert result.overflow_count == 1
    assert result.complete is False


def test_mixed_unknown_total_preserves_observations_without_fabricated_nn() -> None:
    """Unknown-total mixed rows retain both evidence classes without inventing N or overflow."""
    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page(
            [
                {"rank": 1, "noteId": "accepted-note"},
                {"rank": 2, "userId": "account-b", "noteTitle": "无发布时间"},
            ]
        )
    ).collect_rankings(CollectionRequest(capability="rankings"))

    assert result.status == "needs_human"
    assert result.detail == "response_unusable"
    assert [item.id for item in result.items] == ["accepted-note"]
    assert len(result.rejected_items) == 1
    assert result.observed_count == 2
    assert result.expected_count_known is False
    assert result.expected_count is None
    assert result.missing_items == []
    assert result.overflow_count == 0
    assert result.complete is False


def test_fallback_identity_includes_content_type_discriminator() -> None:
    """Image and video notes sharing user, title and publication time must not collide."""
    common = {
        "userId": "same-account",
        "noteTitle": "同题内容",
        "publishTime": "2026-08-17 09:00",
    }
    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page(
            [
                {**common, "rank": 1, "contentType": "image"},
                {**common, "rank": 2, "contentType": "video"},
            ]
        )
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=2))

    assert result.status == "succeeded"
    assert len(result.items) == 2
    assert result.items[0].id != result.items[1].id


def test_fallback_identity_requires_publication_time() -> None:
    """User and title alone are not a stable note identity and must remain rejected evidence."""
    raw_row = {
        "rank": 1,
        "userId": "account-a",
        "noteTitle": "可能重复的标题",
        "contentType": "image",
    }
    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page([raw_row])
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=1))

    assert result.status == "needs_human"
    assert result.items == []
    assert result.succeeded_count == 0
    assert result.observed_count == 1
    assert result.missing_items == []
    assert result.rejected_items[0].reason == "identity_insufficient"
    assert result.rejected_items[0].raw_evidence["item"] == raw_row


@pytest.mark.parametrize(
    "content_type",
    [None, "", "   ", 7, {"kind": "image"}],
    ids=["missing", "empty", "whitespace", "numeric", "object"],
)
def test_fallback_identity_requires_non_empty_string_content_type(
    content_type: object,
) -> None:
    """Fallback identity must reject absent or coerced content types as insufficient."""
    raw_row: dict[str, object] = {
        "rank": 1,
        "userId": "account-a",
        "noteTitle": "同题内容",
        "publishTime": "2026-08-17 09:00",
    }
    if content_type is not None:
        raw_row["contentType"] = content_type

    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page([raw_row])
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=1))

    assert result.status == "needs_human"
    assert result.items == []
    assert result.observed_count == 1
    assert result.missing_items == []
    assert result.rejected_items[0].reason == "identity_insufficient"
    assert result.rejected_items[0].raw_evidence["item"] == raw_row


def test_qianfan_item_identity_keeps_distinct_semantic_notes_from_one_user() -> None:
    """The user ID alone must not collapse different titles or publication times."""
    page = _ranking_page(
        [
            {
                "rank": 1,
                "userId": "same-account",
                "noteTitle": "第一篇",
                "publishTime": "2026-08-17 09:00",
                "contentType": "image",
            },
            {
                "rank": 2,
                "userId": "same-account",
                "noteTitle": "第二篇",
                "publishTime": "2026-08-17 10:00",
                "contentType": "image",
            },
        ]
    )

    result = QianfanPlaywrightAdapter(page_factory=lambda: page).collect_rankings(
        CollectionRequest(capability="rankings", expected_count=2)
    )

    assert result.status == "succeeded"
    assert len(result.items) == 2
    assert result.items[0].id != result.items[1].id


def test_duplicate_canonical_note_identity_is_explicit_rejected_observation() -> None:
    """A colliding source row must retain evidence and count as rejected, never disappear."""
    page = _ranking_page(
        [
            {
                "rank": 1,
                "noteUrl": "https://www.xiaohongshu.com/explore/url-note?xsec_token=one",
                "userId": "account-a",
                "noteTitle": "旧标题",
            },
            {
                "rank": 8,
                "noteUrl": "https://www.xiaohongshu.com/explore/url-note?xsec_token=two",
                "userId": "account-a",
                "noteTitle": "新标题",
            },
        ]
    )

    result = QianfanPlaywrightAdapter(page_factory=lambda: page).collect_rankings(
        CollectionRequest(capability="rankings", expected_count=2)
    )

    assert result.status == "needs_human"
    assert result.succeeded_count == 1
    assert result.observed_count == 2
    assert result.missing_items == []
    assert result.items[0].id.startswith("url:")
    assert str(result.items[0].source_url) == (
        "https://www.xiaohongshu.com/explore/url-note"
    )
    assert len(result.rejected_items) == 1
    assert result.rejected_items[0].reference == "qianfan_response:1:item:2"
    assert result.rejected_items[0].reason == "duplicate_identity"
    assert result.rejected_items[0].raw_evidence["item"]["rank"] == 8


def test_relative_note_url_is_resolved_and_claimed_job_succeeds(tmp_path: Path) -> None:
    """A platform-relative note URL must become a stable absolute source before validation."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    page = _ranking_page([{"rank": 1, "noteUrl": "/explore/n1"}])
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: page, job_service=jobs, runtime_dir=runtime_dir
    )

    result = adapter.collect_rankings(
        CollectionRequest(
            capability="rankings",
            parameters={"job_id": job.id},
            expected_count=1,
        )
    )

    assert result.status == "succeeded"
    assert result.items[0].id == "url:https://www.xiaohongshu.com/explore/n1"
    assert str(result.items[0].source_url) == "https://www.xiaohongshu.com/explore/n1"
    persisted = jobs.get(job.id)
    assert persisted.state is JobState.succeeded
    assert len(persisted.artifacts) == 1


@pytest.mark.parametrize(
    "unsafe_relative_url",
    ["/explore/../evil", "/explore/n1/extra", "/prefix/explore/n1"],
    ids=["dot-segment", "extra-segment", "unknown-prefix"],
)
def test_relative_note_url_requires_one_safe_id_on_a_known_route(
    unsafe_relative_url: str,
) -> None:
    """Relative URLs outside an exact known note route must remain rejected evidence."""
    raw_row = {"rank": 1, "noteUrl": unsafe_relative_url}

    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page([raw_row])
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=1))

    assert result.status == "needs_human"
    assert result.items == []
    assert result.observed_count == 1
    assert result.missing_items == []
    assert result.rejected_items[0].reason == "malformed_note_url"
    assert result.rejected_items[0].raw_evidence["item"] == raw_row


@pytest.mark.parametrize(
    "unsafe_note_id",
    [{"id": "nested"}, "../evil", "has/slash", "dot.segment"],
    ids=["object", "dot-segment", "slash", "dot"],
)
def test_note_id_must_be_a_scalar_safe_token(unsafe_note_id: object) -> None:
    """Unsafe or structured note IDs must be rejected instead of becoming fabricated URLs."""
    raw_row = {"rank": 1, "noteId": unsafe_note_id}

    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page([raw_row])
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=1))

    assert result.status == "needs_human"
    assert result.items == []
    assert result.observed_count == 1
    assert result.missing_items == []
    assert result.rejected_items[0].reason == "malformed_note_id"
    assert result.rejected_items[0].raw_evidence["item"] == raw_row


def test_absolute_https_note_url_on_platform_subdomain_is_accepted() -> None:
    """A real HTTPS Xiaohongshu subdomain URL is a valid canonical identity source."""
    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page(
            [
                {
                    "rank": 1,
                    "noteUrl": "  https://ark.xiaohongshu.com/item/n1?token=private  ",
                }
            ]
        )
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=1))

    assert result.status == "succeeded"
    assert result.items[0].id == "url:https://www.xiaohongshu.com/item/n1"
    assert str(result.items[0].source_url) == "https://www.xiaohongshu.com/item/n1"


@pytest.mark.parametrize(
    "unsafe_url",
    [
        {"path": "/explore/n1"},
        ["/explore/n1"],
        123,
        "www.xiaohongshu.com/explore/n1",
        "//www.xiaohongshu.com/explore/n1",
        "http://www.xiaohongshu.com/explore/n1",
        "https://example.com/explore/n1",
        "https://user:secret@www.xiaohongshu.com/explore/n1",
        "https://www.xiaohongshu.com/explore/n1#unsafe",
        "https://www.xiaohongshu.com/explore/n 1",
        "http://[",
    ],
    ids=[
        "dict",
        "list",
        "numeric",
        "scheme-less-host",
        "network-path",
        "http",
        "foreign-host",
        "credentials",
        "fragment",
        "space",
        "malformed",
    ],
)
def test_untrusted_note_url_is_rejected_without_string_coercion(unsafe_url: object) -> None:
    """Only an actual safe platform URL string may become collection identity or source."""
    raw_row = {"rank": 1, "noteUrl": unsafe_url}
    result = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page([raw_row])
    ).collect_rankings(CollectionRequest(capability="rankings", expected_count=1))

    assert result.status == "needs_human"
    assert result.items == []
    assert result.observed_count == 1
    assert result.missing_items == []
    assert result.rejected_items[0].reason == "malformed_note_url"
    assert result.rejected_items[0].raw_evidence["item"] == raw_row


def test_malformed_note_url_preserves_evidence_and_never_strands_claimed_job(
    tmp_path: Path,
) -> None:
    """An unusable note URL must become a reviewable terminal fact, not a validation crash."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    raw_row = {"rank": 1, "noteUrl": "http://["}
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page([raw_row]),
        job_service=jobs,
        runtime_dir=runtime_dir,
    )

    result = adapter.collect_rankings(
        CollectionRequest(
            capability="rankings",
            parameters={"job_id": job.id},
            expected_count=1,
        )
    )

    assert result.status == "needs_human"
    assert result.detail == "response_unusable"
    assert result.observed_count == 1
    assert result.missing_items == []
    assert result.rejected_items[0].reason == "malformed_note_url"
    assert result.rejected_items[0].raw_evidence["item"] == raw_row
    persisted = jobs.get(job.id)
    assert persisted.state is JobState.needs_human
    assert persisted.error_category == "response_unusable"
    assert len(persisted.artifacts) == 1
    evidence = json.loads(
        (runtime_dir / persisted.artifacts[0].path).read_text(encoding="utf-8")
    )
    assert evidence["raw_evidence"]["responses"][0]["body"]["data"]["dataList"] == [
        raw_row
    ]


def test_unexpected_normalization_error_persists_evidence_and_fails_claimed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unexpected post-capture bugs may propagate, but must not abandon the running lease."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    raw_row = {"rank": 1, "noteId": "captured-before-bug"}
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page([raw_row]),
        job_service=jobs,
        runtime_dir=runtime_dir,
    )

    def fail_normalization(_: dict[str, Any]) -> object:
        raise RuntimeError("unexpected normalization bug")

    monkeypatch.setattr(qianfan_module, "_normalized_items", fail_normalization)

    with pytest.raises(RuntimeError, match="unexpected normalization bug"):
        adapter.collect_rankings(
            CollectionRequest(
                capability="rankings",
                parameters={"job_id": job.id},
                expected_count=1,
            )
        )

    persisted = jobs.get(job.id)
    assert persisted.state is JobState.failed
    assert persisted.error_category == "normalization_failed"
    assert len(persisted.artifacts) == 1
    evidence = json.loads(
        (runtime_dir / persisted.artifacts[0].path).read_text(encoding="utf-8")
    )
    assert evidence["raw_evidence"]["responses"][0]["body"]["data"]["dataList"] == [
        raw_row
    ]


def test_unexpected_result_validation_error_preserves_evidence_and_fails_claimed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A propagated contract-validation bug must retain capture and end the running job."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    raw_row = {"rank": 1, "noteId": "captured-before-validation-bug"}
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page([raw_row]),
        job_service=jobs,
        runtime_dir=runtime_dir,
    )

    def fail_validation(**_: object) -> object:
        raise ValueError("unexpected result validation bug")

    monkeypatch.setattr(qianfan_module, "_accounted_result", fail_validation)

    with pytest.raises(ValueError, match="unexpected result validation bug"):
        adapter.collect_rankings(
            CollectionRequest(
                capability="rankings",
                parameters={"job_id": job.id},
                expected_count=1,
            )
        )

    persisted = jobs.get(job.id)
    assert persisted.state is JobState.failed
    assert persisted.error_category == "result_validation_failed"
    assert len(persisted.artifacts) == 1
    evidence = json.loads(
        (runtime_dir / persisted.artifacts[0].path).read_text(encoding="utf-8")
    )
    assert evidence["raw_evidence"]["responses"][0]["body"]["data"]["dataList"] == [
        raw_row
    ]


def test_collect_rankings_maps_login_to_needs_human_job_and_contract(
    tmp_path: Path,
) -> None:
    """A login wall must agree across collection result, durable job state and evidence artifact."""
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    page = _FakePage(
        url="https://ark.xiaohongshu.com/login",
        html="<form>登录</form>",
        selectors={"input[type='password']": 1},
    )
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: page,
        job_service=jobs,
        runtime_dir=runtime_dir,
        timeout_seconds=0,
    )

    result = adapter.collect_rankings(
        CollectionRequest(capability="rankings", parameters={"job_id": job.id})
    )

    assert result.status == "needs_human"
    assert result.detail == "login_required"
    assert result.expected_count_known is False
    assert result.complete is False
    assert result.missing_items == []
    persisted_job = jobs.get(job.id)
    assert persisted_job.state is JobState.needs_human
    assert len(persisted_job.artifacts) == 1


def test_collect_rankings_maps_layout_timeout_to_needs_human_job(
    tmp_path: Path,
) -> None:
    """Missing layout and XHR evidence must terminally pause the claimed job for review."""
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    page = _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<main>changed</main>",
        selectors={"input[type='password']": 0, ".note-rank table": 0},
    )
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: page,
        job_service=jobs,
        runtime_dir=runtime_dir,
        timeout_seconds=0,
    )

    result = adapter.collect_rankings(
        CollectionRequest(
            capability="rankings", parameters={"job_id": job.id}, expected_count=1
        )
    )

    assert result.status == "needs_human"
    assert result.detail == "layout_changed"
    persisted = jobs.get(job.id)
    assert persisted.state is JobState.needs_human
    assert persisted.error_category == "layout_changed"


def test_collect_rankings_without_expected_count_is_incomplete_and_needs_human_job(
    tmp_path: Path,
) -> None:
    """Observed rows cannot establish N/N when the caller did not supply a known total."""
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    page = _ranking_page([{"rank": 1, "noteId": "unknown-total"}])
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: page, job_service=jobs, runtime_dir=runtime_dir
    )

    result = adapter.collect_rankings(
        CollectionRequest(capability="rankings", parameters={"job_id": job.id})
    )

    assert result.status == "partial"
    assert result.detail == "expected_count_unknown"
    assert result.expected_count_known is False
    assert result.expected_count is None
    assert result.succeeded_count == 1
    assert result.overflow_count == 0
    assert result.complete is False
    persisted = jobs.get(job.id)
    assert persisted.state is JobState.needs_human
    assert persisted.error_category == "expected_count_unknown"


def test_valid_empty_response_with_known_zero_is_complete_and_succeeds_job(
    tmp_path: Path,
) -> None:
    """A structurally valid empty ranking list truthfully satisfies a caller-known zero total."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page([]),
        job_service=jobs,
        runtime_dir=runtime_dir,
    )

    result = adapter.collect_rankings(
        CollectionRequest(
            capability="rankings",
            parameters={"job_id": job.id},
            expected_count=0,
        )
    )

    assert result.status == "succeeded"
    assert result.expected_count_known is True
    assert result.expected_count == 0
    assert result.succeeded_count == 0
    assert result.items == []
    assert result.missing_items == []
    assert result.complete is True
    assert jobs.get(job.id).state is JobState.succeeded


def test_http_error_empty_response_cannot_satisfy_known_zero_and_is_terminal(
    tmp_path: Path,
) -> None:
    """HTTP 500 with an empty-shaped body is transport failure evidence, never 0/0 success."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    page = _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<section class='note-rank'><table></table></section>",
        selectors={"input[type='password']": 0, ".note-rank table": 1},
        responses=[
            _FakeResponse(
                "https://ark.xiaohongshu.com/api/edith/business/data/note/rank/v2/list",
                500,
                {"data": {"dataList": []}},
            )
        ],
    )
    result = QianfanPlaywrightAdapter(
        page_factory=lambda: page,
        job_service=jobs,
        runtime_dir=runtime_dir,
    ).collect_rankings(
        CollectionRequest(
            capability="rankings",
            parameters={"job_id": job.id},
            expected_count=0,
        )
    )

    assert result.status == "needs_human"
    assert result.detail == "response_unusable"
    assert result.complete is False
    assert result.observed_count == 0
    assert jobs.get(job.id).state is JobState.needs_human
    assert jobs.get(job.id).artifacts


@pytest.mark.parametrize(
    "business_marker",
    [{"code": 500}, {"status": "failed"}],
    ids=["code", "status"],
)
def test_business_error_empty_response_cannot_satisfy_known_zero(
    business_marker: dict[str, object],
) -> None:
    """A recognized non-success business marker makes an HTTP-200 payload unusable."""
    body = {**business_marker, "data": {"dataList": []}}
    page = _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<section class='note-rank'><table></table></section>",
        selectors={"input[type='password']": 0, ".note-rank table": 1},
        responses=[
            _FakeResponse(
                "https://ark.xiaohongshu.com/api/edith/business/data/note/rank/v2/list",
                200,
                body,
            )
        ],
    )

    result = QianfanPlaywrightAdapter(page_factory=lambda: page).collect_rankings(
        CollectionRequest(capability="rankings", expected_count=0)
    )

    assert result.status == "needs_human"
    assert result.detail == "response_unusable"
    assert result.complete is False
    assert result.observed_count == 0


def test_business_success_code_allows_known_zero_completion() -> None:
    """A recognized success business code plus HTTP 2xx permits literal empty-list accounting."""
    page = _FakePage(
        url="https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        html="<section class='note-rank'><table></table></section>",
        selectors={"input[type='password']": 0, ".note-rank table": 1},
        responses=[
            _FakeResponse(
                "https://ark.xiaohongshu.com/api/edith/business/data/note/rank/v2/list",
                200,
                {"code": 0, "data": {"dataList": []}},
            )
        ],
    )

    result = QianfanPlaywrightAdapter(page_factory=lambda: page).collect_rankings(
        CollectionRequest(capability="rankings", expected_count=0)
    )

    assert result.status == "succeeded"
    assert result.complete is True
    assert result.observed_count == 0


def test_valid_empty_response_with_unknown_total_needs_human(
    tmp_path: Path,
) -> None:
    """An observed empty list cannot establish completion when the caller supplied no total."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: _ranking_page([]),
        job_service=jobs,
        runtime_dir=runtime_dir,
    )

    result = adapter.collect_rankings(
        CollectionRequest(capability="rankings", parameters={"job_id": job.id})
    )

    assert result.status == "needs_human"
    assert result.detail == "expected_count_unknown"
    assert result.expected_count_known is False
    assert result.succeeded_count == 0
    assert result.complete is False
    persisted = jobs.get(job.id)
    assert persisted.state is JobState.needs_human
    assert persisted.error_category == "expected_count_unknown"
    assert len(persisted.artifacts) == 1


def test_partial_collection_moves_claimed_job_to_needs_human(tmp_path: Path) -> None:
    """A known missing row must produce partial facts and never leave the job running."""
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    page = _ranking_page([{"rank": 1, "noteId": "one-of-two"}])
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: page, job_service=jobs, runtime_dir=runtime_dir
    )

    result = adapter.collect_rankings(
        CollectionRequest(
            capability="rankings", parameters={"job_id": job.id}, expected_count=2
        )
    )

    assert result.status == "partial"
    assert result.detail == "expected_items_missing"
    assert result.expected_count_known is True
    assert result.overflow_count == 0
    assert result.complete is False
    persisted = jobs.get(job.id)
    assert persisted.state is JobState.needs_human
    assert persisted.error_category == "expected_items_missing"


def test_observed_count_over_expected_fails_result_and_job(tmp_path: Path) -> None:
    """More observed rows than the caller's known total is contradictory and must fail."""
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    page = _ranking_page(
        [
            {"rank": 1, "noteId": "unexpected-a"},
            {"rank": 2, "noteId": "unexpected-b"},
        ]
    )
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: page, job_service=jobs, runtime_dir=runtime_dir
    )

    result = adapter.collect_rankings(
        CollectionRequest(
            capability="rankings", parameters={"job_id": job.id}, expected_count=1
        )
    )

    assert result.status == "failed"
    assert result.detail == "observed_count_exceeds_expected"
    assert result.expected_count_known is True
    assert result.expected_count == 1
    assert result.succeeded_count == 2
    assert len(result.items) == 2
    assert result.missing_items == []
    assert result.overflow_count == 1
    assert result.complete is False
    persisted = jobs.get(job.id)
    assert persisted.state is JobState.failed
    assert persisted.error_category == "observed_count_exceeds_expected"


@pytest.mark.parametrize(
    "job_id",
    ["../../../../escaped", "not-a-uuid"],
)
def test_invalid_job_id_is_rejected_before_any_evidence_write(
    tmp_path: Path, job_id: str
) -> None:
    """A path-like or noncanonical job id must never influence a filesystem target."""
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime_dir)
    page = _ranking_page([{"rank": 1, "noteId": "safe-note"}])
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: page, job_service=jobs, runtime_dir=runtime_dir
    )

    with pytest.raises(ValueError, match="canonical UUID"):
        adapter.collect_rankings(
            CollectionRequest(
                capability="rankings",
                parameters={"job_id": job_id},
                expected_count=1,
            )
        )

    assert not (runtime_dir / "evidence").exists()
    assert list(tmp_path.glob("escaped*.json")) == []


def test_nonexistent_job_is_rejected_before_any_evidence_write(tmp_path: Path) -> None:
    """A well-formed but unknown job UUID must be checked before mkdir or file creation."""
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime_dir)
    page = _ranking_page([{"rank": 1, "noteId": "safe-note"}])
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: page, job_service=jobs, runtime_dir=runtime_dir
    )

    with pytest.raises(JobNotFound, match="does not exist"):
        adapter.collect_rankings(
            CollectionRequest(
                capability="rankings",
                parameters={"job_id": str(uuid4())},
                expected_count=1,
            )
        )

    assert not (runtime_dir / "evidence").exists()
