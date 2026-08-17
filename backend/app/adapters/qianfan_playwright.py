"""Controlled, read-only capture boundary for the login-dependent Qianfan page."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field


QIANFAN_RANK_URL = "https://ark.xiaohongshu.com/app-datacenter/market/note-rank"


class QianfanCaptureOutcome(BaseModel):
    status: Literal["captured", "needs_human"]
    reason: Literal["login_required", "layout_changed"] | None = None
    raw_evidence: dict[str, Any] = Field(min_length=1)


class QianfanPlaywrightAdapter:
    """Navigate only to the ranking page and preserve observed page/network facts."""

    def __init__(self, page_factory: Callable[[], Any]) -> None:
        self._page_factory = page_factory

    def capture_visible_page(self) -> QianfanCaptureOutcome:
        page = self._page_factory()
        responses: list[dict[str, Any]] = []
        if hasattr(page, "on"):
            page.on("response", lambda response: _record_response(response, responses))
        page.goto(QIANFAN_RANK_URL, wait_until="domcontentloaded")
        html = page.content()
        raw_evidence = {
            "page_url": str(page.url),
            "page_html": html,
            "responses": responses,
        }
        if "/login" in str(page.url) or page.locator("input[type='password']").count() > 0:
            return QianfanCaptureOutcome(
                status="needs_human", reason="login_required", raw_evidence=raw_evidence
            )
        if page.locator(".note-rank table").count() == 0:
            return QianfanCaptureOutcome(
                status="needs_human", reason="layout_changed", raw_evidence=raw_evidence
            )
        return QianfanCaptureOutcome(status="captured", raw_evidence=raw_evidence)


def _record_response(response: Any, sink: list[dict[str, Any]]) -> None:
    url = str(response.url)
    if "/api/edith/business/data/note/" not in url:
        return
    try:
        body: Any = response.json()
    except Exception as error:
        body = {"capture_error": type(error).__name__}
    sink.append({"url": url, "status": int(response.status), "body": body})
