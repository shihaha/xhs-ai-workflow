from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.app.adapters.contracts import CollectionRequest
from backend.app.adapters.qianfan_playwright import (
    DEFAULT_QIANFAN_SELECTOR_PROFILE,
    QIANFAN_RANK_URL,
    QianfanPlaywrightAdapter,
)
from backend.app.db import Database
from backend.app.services.jobs import JobService


class _LiveRequest:
    def __init__(self, *, url: str, data: dict[str, Any]) -> None:
        self.url = url
        self.method = "POST"
        self._data = data

    def post_data_json(self) -> dict[str, Any]:
        return self._data


class _LiveResponse:
    status = 200

    def __init__(self, *, path: str, data: dict[str, Any], body: dict[str, Any]) -> None:
        self.url = f"https://ark.xiaohongshu.com{path}?transient=not-persisted"
        self.request = _LiveRequest(url=self.url, data=data)
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _LiveLocator:
    def __init__(self, page: "_LivePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    def count(self) -> int:
        if self.selector in {
            DEFAULT_QIANFAN_SELECTOR_PROFILE.login_selector,
            DEFAULT_QIANFAN_SELECTOR_PROFILE.captcha_selector,
        }:
            return 0
        return 1

    def click(self) -> None:
        self.page.clicks.append(self.selector)
        if len(self.page.clicks) % 2 == 0 and self.page.handler is not None:
            for response in self.page.responses:
                self.page.handler(response)


class _LivePage:
    url = QIANFAN_RANK_URL

    def __init__(self, responses: list[_LiveResponse]) -> None:
        self.responses = responses
        self.handler: Any = None
        self.clicks: list[str] = []

    def on(self, event: str, handler: Any) -> None:
        assert event == "response"
        self.handler = handler

    def remove_listener(self, _event: str, _handler: Any) -> None:
        self.handler = None

    def goto(self, url: str, *, wait_until: str) -> None:
        assert url == QIANFAN_RANK_URL
        assert wait_until == "domcontentloaded"

    def locator(self, selector: str) -> _LiveLocator:
        return _LiveLocator(self, selector)

    def content(self) -> str:
        return "<main>authenticated current Qianfan layout</main>"


def test_live_profile_accepts_only_canonical_content_response_bound_to_active_scope() -> None:
    """Wrong request facts or a page-size-one helper response must not satisfy this scope."""
    page = _LivePage(
        [
            _LiveResponse(
                path="/api/edith/business/data/note/rank/v2/list",
                data={"sortBy": 1, "noteType": 0, "pageNo": 1, "pageSize": 1},
                body={"data": {"dataList": [{"rank": 1, "noteId": "helper"}]}},
            ),
            _LiveResponse(
                path="/api/edith/business/data/note/rank/v2/list",
                data={"sortBy": 1, "noteType": 0, "pageNo": 1, "pageSize": 10},
                body={
                    "code": 0,
                    "data": {"dataList": [{"rank": 1, "noteId": "canonical-note"}]},
                },
            ),
        ]
    )

    result = QianfanPlaywrightAdapter(page_factory=lambda: page, timeout_seconds=0).collect_scope(
        CollectionRequest(capability="rankings", expected_count=1),
        board="阅读榜",
        dimension="优秀内容",
        selector_profile=DEFAULT_QIANFAN_SELECTOR_PROFILE,
    )

    assert DEFAULT_QIANFAN_SELECTOR_PROFILE.supported is True
    assert result.status == "succeeded"
    assert [item.id for item in result.items] == ["canonical-note"]
    assert "?" not in result.items[0].raw_evidence["response_url"]


def test_live_profile_normalizes_account_endpoint_without_note_facts_or_tokens(
    tmp_path: Path,
) -> None:
    """Account rankings require a safe account identity and never retain credential-like fields."""
    page = _LivePage(
        [
            _LiveResponse(
                path="/api/edith/business/data/note/user/rank/v2/list",
                data={"sortBy": 4, "noteType": 0, "pageNo": 1, "pageSize": 10},
                body={
                    "code": 0,
                    "data": {
                        "dataList": [
                            {
                                "rank": 2,
                                "userId": "account_7",
                                "userNickname": "真实账号",
                                "userFansNum": 321,
                                "xsec_token": "must-not-survive",
                                "nested": {"accessToken": "also-not-persisted"},
                            }
                        ]
                    },
                },
            )
        ]
    )

    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="qianfan_rankings", input_data={})
    jobs.claim(job.id)
    adapter = QianfanPlaywrightAdapter(
        page_factory=lambda: page,
        timeout_seconds=0,
        job_service=jobs,
        runtime_dir=runtime_dir,
    )
    outcome = adapter.capture_scope_page(
        board="成交榜",
        dimension="优秀账号",
        selector_profile=DEFAULT_QIANFAN_SELECTOR_PROFILE,
    )
    result = adapter.collect_scope(
        CollectionRequest(
            capability="rankings",
            expected_count=1,
            parameters={"job_id": job.id},
        ),
        board="成交榜",
        dimension="优秀账号",
        selector_profile=DEFAULT_QIANFAN_SELECTOR_PROFILE,
    )

    item = result.items[0]
    assert result.status == "succeeded"
    assert item.id == "account:account_7"
    assert str(item.source_url) == "https://www.xiaohongshu.com/user/profile/account_7"
    assert item.data == {"rank": 2, "author_name": "真实账号", "user_id": "account_7"}
    assert "note_id" not in item.data
    assert "xsec_token" not in str(outcome.raw_evidence)
    assert "xsec_token" not in str(item.raw_evidence)
    assert "accessToken" not in str(item.raw_evidence)
    persisted = json.loads(
        (runtime_dir / jobs.get(job.id).artifacts[0].path).read_text(encoding="utf-8")
    )
    assert "xsec_token" not in str(persisted)
    assert "accessToken" not in str(persisted)


@pytest.mark.parametrize(
    "request_data",
    [
        {"sortBy": 2, "noteType": 0, "pageNo": 1, "pageSize": 10},
        {"sortBy": 1, "noteType": 1, "pageNo": 1, "pageSize": 10},
        {"sortBy": 1, "noteType": 0, "pageNo": 2, "pageSize": 10},
        {"sortBy": 1, "noteType": 0, "pageNo": 1, "pageSize": 1},
    ],
    ids=["wrong-board", "wrong-note-type", "wrong-page", "helper-page-size"],
)
def test_live_profile_rejects_request_scope_mismatch(request_data: dict[str, Any]) -> None:
    """A response cannot establish scope from caller labels when its request facts disagree."""
    page = _LivePage(
        [
            _LiveResponse(
                path="/api/edith/business/data/note/rank/v2/list",
                data=request_data,
                body={"code": 0, "data": {"dataList": [{"rank": 1, "noteId": "wrong"}]}},
            )
        ]
    )

    result = QianfanPlaywrightAdapter(page_factory=lambda: page, timeout_seconds=0).collect_scope(
        CollectionRequest(capability="rankings", expected_count=1),
        board="阅读榜",
        dimension="优秀内容",
        selector_profile=DEFAULT_QIANFAN_SELECTOR_PROFILE,
    )

    assert result.status == "needs_human"
    assert result.detail == "scope_unverified"


def test_supported_profile_rejects_incomplete_endpoint_mapping() -> None:
    """A supported version without both response endpoints is not a verified live contract."""
    with pytest.raises(ValueError, match="every fixed scope"):
        DEFAULT_QIANFAN_SELECTOR_PROFILE.model_copy(
            update={"response_endpoint_paths": (("优秀内容", "/content"),)}
        ).validate_supported_profile()
