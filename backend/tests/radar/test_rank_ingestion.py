import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

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


def test_malformed_response_preserves_raw_body_and_parse_error() -> None:
    """A JSON parse failure must not replace the actual provider response evidence."""
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
    assert response["raw_text"] == "<html>bad gateway</html>"
    assert response["capture_error"] == "ValueError"


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
    assert result.complete is False
    assert result.missing_items[0].reason == "response_not_observed"


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
    assert result.complete is False
    assert result.missing_items[0].reason == "login_required"
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
    assert result.expected_count is None
    assert result.succeeded_count == 1
    assert result.complete is False
    persisted = jobs.get(job.id)
    assert persisted.state is JobState.needs_human
    assert persisted.error_category == "expected_count_unknown"


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
