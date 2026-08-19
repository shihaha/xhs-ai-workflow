from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Timer
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image

from backend.app.adapters.android_device import (
    DEFAULT_SELECTOR_PROFILE_VERSION,
    AndroidDeviceAdapter,
    parse_shop_hierarchy,
)
from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    DeviceHealth,
)
from backend.app.db import Database
from backend.app.features.shops.service import ShopCollectionService
from backend.app.main import create_app
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService
from backend.app.settings import Settings


PROFILE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy><node text="账号主页" bounds="[0,0][720,1600]" /></hierarchy>"""
EMPTY_PROFILE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy><node text="账号主页" bounds="[0,0][720,1600]" /></hierarchy>"""
LOGIN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy><node text="手机号登录" bounds="[0,0][720,1600]" /></hierarchy>"""
CAPTCHA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy><node text="请完成安全验证" bounds="[0,0][720,1600]" /></hierarchy>"""
SHOP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node text="高质量课程资料合集" bounds="[20,120][600,180]" />
  <node text="当月热销第3名" bounds="[20,185][300,220]" />
  <node content-desc="到手价¥19.90已售1.2万+" bounds="[20,225][600,280]" />
  <node text="没有更多商品了" bounds="[0,1400][720,1500]" />
</hierarchy>"""
DETAIL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node text="高质量课程资料合集" bounds="[20,100][600,180]" />
  <node text="立即购买" bounds="[400,1450][700,1580]" />
  <node content-desc="分享商品" bounds="[620,20][710,110]" />
</hierarchy>"""
SHARE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy><node content-desc="复制链接" bounds="[120,1300][360,1500]" /></hierarchy>"""
SAME_TITLE_SHOP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node text="同名商品完整标题" bounds="[20,120][600,180]" />
  <node content-desc="到手价¥19.90已售100+" bounds="[20,225][600,280]" />
  <node text="同名商品完整标题" bounds="[20,420][600,480]" />
  <node content-desc="到手价¥29.90已售200+" bounds="[20,525][600,580]" />
  <node text="没有更多商品了" bounds="[0,1400][720,1500]" />
</hierarchy>"""


class _FakeAdbDevice:
    def __init__(self, serial: str = "phone-1") -> None:
        self.serial = serial

    def get_state(self) -> str:
        return "device"


class _FakeAdbClient:
    def device_list(self) -> list[_FakeAdbDevice]:
        return [_FakeAdbDevice()]


class _FakeUiObject:
    def __init__(self, exists: bool, action: Callable[[], None]) -> None:
        self.exists = exists
        self._action = action

    def click(self) -> None:
        self._action()


class _FakeU2Device:
    def __init__(
        self,
        profile_xml: str = PROFILE_XML,
        *,
        shop_selector_available: bool = True,
        link: str = "https://xhslink.com/product-a",
        on_open_url: Callable[[], None] | None = None,
        on_selector: Callable[[tuple[str, str]], None] | None = None,
        back_screens: list[str] | None = None,
        shop_xml: str = SHOP_XML,
        links_by_y: dict[int, str] | None = None,
        clipboard_before: str = "",
        clipboard_after: list[str] | None = None,
    ) -> None:
        self.screen = "initial"
        self.screens = {
            "profile": profile_xml,
            "shop": shop_xml,
            "detail": DETAIL_XML,
            "share": SHARE_XML,
        }
        self.shop_selector_available = shop_selector_available
        self.link = link
        self.on_open_url = on_open_url
        self.on_selector = on_selector
        self.back_screens = list(back_screens or ["shop"])
        self.links_by_y = dict(links_by_y or {})
        self._clipboard_before = clipboard_before
        self._clipboard_after = (
            list(clipboard_after) if clipboard_after is not None else None
        )
        self._copy_clicked = False
        self.clipboard_reads = 0
        self.actions: list[tuple[object, ...]] = []

    def app_current(self) -> dict[str, object]:
        activities = {
            "detail": "com.xingin.xhs.activity.GoodsDetail",
            "shop": "com.xingin.xhs.activity.ShopDetail",
        }
        return {
            "package": "com.xingin.xhs",
            "activity": activities.get(self.screen, "com.xingin.xhs.activity.NewOtherUser"),
            "pid": 22,
        }

    def open_url(self, url: str) -> None:
        self.actions.append(("open_url", url))
        self.screen = "profile"
        if self.on_open_url is not None:
            self.on_open_url()

    def dump_hierarchy(self, *, compressed: bool = False) -> str:
        _ = compressed
        return self.screens[self.screen]

    def screenshot(self, *, format: str = "raw") -> bytes:
        assert format == "raw"
        return b"\x89PNG\r\n\x1a\nobserved-screen"

    def __call__(self, **query: str) -> _FakeUiObject:
        key = next(iter(query.items()))
        target: str | None = None
        exists = False
        if self.screen == "profile" and key == ("text", "店铺"):
            exists = self.shop_selector_available
            target = "shop"
        elif self.screen == "detail" and key == ("description", "分享商品"):
            exists = True
            target = "share"
        elif self.screen == "share" and key == ("description", "复制链接"):
            exists = True

        def act() -> None:
            self.actions.append(("selector", *key))
            if target is not None:
                self.screen = target
            if key == ("description", "复制链接"):
                self._copy_clicked = True
            if self.on_selector is not None:
                self.on_selector(key)

        return _FakeUiObject(exists, act)

    def click(self, x: int, y: int) -> None:
        self.actions.append(("click", x, y))
        self.link = self.links_by_y.get(y, self.link)
        self._copy_clicked = False
        self.screen = "detail"

    def press(self, key: str) -> None:
        self.actions.append(("press", key))
        self.screen = self.back_screens.pop(0) if self.back_screens else "shop"

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float) -> None:
        self.actions.append(("swipe", x1, y1, x2, y2, duration))

    @property
    def clipboard(self) -> str:
        self.clipboard_reads += 1
        if not self._copy_clicked:
            return self._clipboard_before
        if self._clipboard_after is None:
            return self.link
        if len(self._clipboard_after) > 1:
            return self._clipboard_after.pop(0)
        return self._clipboard_after[0]


def _adapter(
    tmp_path: Path,
    device: _FakeU2Device,
    *,
    jobs: JobService | None = None,
    **adapter_options: Any,
) -> AndroidDeviceAdapter:
    adapter_options.setdefault("profile_settle_seconds", 0)
    adapter_options.setdefault("selector_timeout_seconds", 0)
    adapter_options.setdefault("sleep", lambda _: None)
    return AndroidDeviceAdapter(
        runtime_dir=tmp_path / "runtime",
        job_service=jobs,
        adb_client_factory=_FakeAdbClient,
        u2_connector=lambda _: device,
        executable_resolver=lambda _: "C:/Android/platform-tools/adb.exe",
        max_shop_screens=2,
        **adapter_options,
    )


def test_tutorial_shop_parser_keeps_title_price_sales_rank_and_coordinates() -> None:
    """The ported hierarchy parser must preserve the facts used to open a product."""
    products = parse_shop_hierarchy(SHOP_XML)

    assert len(products) == 1
    product = products[0]
    assert product.title == "高质量课程资料合集"
    assert product.price == "¥19.90"
    assert product.sold == "1.2万+"
    assert product.rank == "当月热销第3名"
    assert (product.center_x, product.center_y) == (310, 150)


def test_live_uiautomator_screenshot_default_pillow_shape_is_accepted(
    tmp_path: Path,
) -> None:
    """The pinned uiautomator2 API defaults to Pillow and rejects the old raw format."""

    class PillowScreenshotDevice(_FakeU2Device):
        def screenshot(self, *, format: str = "pillow") -> Image.Image:
            if format != "pillow":
                raise ValueError(("Unsupported format:", format))
            return Image.new("RGB", (2, 2), color="red")

    result = _adapter(tmp_path, PillowScreenshotDevice()).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "succeeded"
    assert result.complete is True


def test_profile_navigation_targets_xhs_package_when_device_shell_is_available(
    tmp_path: Path,
) -> None:
    """A system browser must not intercept the trusted XHS profile deep link."""

    class PackageBoundNavigationDevice(_FakeU2Device):
        def shell(self, argv: list[str]) -> None:
            self.actions.append(("shell", *argv))
            self.screen = "profile"

    device = PackageBoundNavigationDevice()
    result = _adapter(tmp_path, device).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "succeeded"
    assert device.actions[0] == (
        "shell",
        "am",
        "start",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        "https://www.xiaohongshu.com/user/profile/account-1",
        "-p",
        "com.xingin.xhs",
    )


@pytest.mark.parametrize(
    ("profile_xml", "detail"),
    [(LOGIN_XML, "login_required"), (CAPTCHA_XML, "captcha_required")],
    ids=["login", "captcha"],
)
def test_collection_stops_for_login_or_captcha_without_bypass(
    tmp_path: Path, profile_xml: str, detail: str
) -> None:
    """Authentication and challenge screens require a human and receive no bypass click."""
    device = _FakeU2Device(profile_xml)

    result = _adapter(tmp_path, device).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "needs_human"
    assert result.detail == detail
    assert result.succeeded_count == 0
    assert result.observed_count == 0
    assert len(result.missing_items) == 1
    assert not any(action[0] in {"selector", "click"} for action in device.actions)


def test_collection_reports_changed_profile_selector_instead_of_empty_success(
    tmp_path: Path,
) -> None:
    """A changed Shop selector is a needs-human layout fact, not a zero-product shop."""
    device = _FakeU2Device(EMPTY_PROFILE_XML, shop_selector_available=False)

    result = _adapter(tmp_path, device).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "needs_human"
    assert result.detail == "selector_changed"
    assert result.items == []
    assert result.observed_count == 0
    assert len(result.missing_items) == 1


def test_collection_waits_for_delayed_shop_hierarchy_before_selector_ruling(
    tmp_path: Path,
) -> None:
    """A bounded delayed UI dump must reach the shop instead of becoming a false layout change."""

    class DelayedShopDevice(_FakeU2Device):
        def __init__(self) -> None:
            super().__init__()
            self.shop_dumps = 0

        def dump_hierarchy(self, *, compressed: bool = False) -> str:
            if self.screen == "shop":
                self.shop_dumps += 1
                if self.shop_dumps == 1:
                    return PROFILE_XML
            return super().dump_hierarchy(compressed=compressed)

    device = DelayedShopDevice()

    result = _adapter(
        tmp_path,
        device,
        transition_poll_interval=0,
    ).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "succeeded"
    assert device.shop_dumps >= 2


def test_collection_reports_device_disconnect_during_profile_transition(
    tmp_path: Path,
) -> None:
    """A phone lost after navigation must fail with all expected products still named missing."""

    class DisconnectingDevice(_FakeU2Device):
        def dump_hierarchy(self, *, compressed: bool = False) -> str:
            _ = compressed
            raise ConnectionError("device offline")

    result = _adapter(tmp_path, DisconnectingDevice()).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "failed"
    assert result.detail == "device_disconnected"
    assert result.succeeded_count == 0
    assert result.observed_count == 0
    assert len(result.missing_items) == 1
    assert result.missing_items[0].reason == "device_disconnected"


def test_collection_honors_in_flight_job_cancellation_at_transition_checkpoint(
    tmp_path: Path,
) -> None:
    """A job cancelled during navigation must stop before shop interaction and stay cancelled."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="shop_collection", input_data={}, progress_total=1)
    jobs.claim(job.id)
    device = _FakeU2Device(
        on_open_url=lambda: jobs.transition(job.id, JobState.cancelled)
    )

    result = _adapter(tmp_path, device, jobs=jobs).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1", "job_id": job.id},
            expected_count=1,
        )
    )

    assert result.status == "failed"
    assert result.detail == "cancelled"
    assert result.succeeded_count == 0
    assert result.observed_count == 0
    assert len(result.missing_items) == 1
    assert jobs.get(job.id).state is JobState.cancelled
    assert not any(action[0] == "selector" for action in device.actions)


def test_collection_honors_external_worker_shutdown_callback(
    tmp_path: Path,
) -> None:
    """Shutdown signaling must stop Android work even when database polling is unavailable."""
    shutdown_requested = Event()
    device = _FakeU2Device(on_open_url=shutdown_requested.set)

    result = _adapter(tmp_path, device).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={
                "account_user_id": "account-1",
                "is_cancelled": shutdown_requested.is_set,
            },
            expected_count=1,
        )
    )

    assert result.status == "failed"
    assert result.detail == "cancelled"
    assert not any(action[0] == "selector" for action in device.actions)


def test_collection_stops_immediately_when_cancelled_on_share_transition(
    tmp_path: Path,
) -> None:
    """Cancellation after entering Share must prevent copy-link and back interactions."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="shop_collection", input_data={}, progress_total=1)
    jobs.claim(job.id)

    def cancel_on_share(selector: tuple[str, str]) -> None:
        if selector == ("description", "分享商品"):
            jobs.transition(job.id, JobState.cancelled)

    device = _FakeU2Device(on_selector=cancel_on_share)

    result = _adapter(tmp_path, device, jobs=jobs).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1", "job_id": job.id},
            expected_count=1,
        )
    )

    assert result.status == "failed"
    assert result.detail == "cancelled"
    assert jobs.get(job.id).state is JobState.cancelled
    assert ("selector", "description", "复制链接") not in device.actions
    assert ("press", "back") not in device.actions


def test_collection_stops_after_copy_cancellation_before_accepting_clipboard(
    tmp_path: Path,
) -> None:
    """Accepting a freshly copied URL or pressing Back after cancellation is a race."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="shop_collection", input_data={}, progress_total=1)
    jobs.claim(job.id)

    def cancel_on_copy(selector: tuple[str, str]) -> None:
        if selector == ("description", "复制链接"):
            jobs.transition(job.id, JobState.cancelled)

    device = _FakeU2Device(on_selector=cancel_on_copy)

    result = _adapter(tmp_path, device, jobs=jobs).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1", "job_id": job.id},
            expected_count=1,
        )
    )

    assert result.status == "failed"
    assert result.detail == "cancelled"
    assert result.items == []
    assert ("press", "back") not in device.actions


def test_collection_rechecks_cancellation_immediately_before_back(
    tmp_path: Path,
) -> None:
    """A state change during the return check must prevent the pending Back action."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="shop_collection", input_data={}, progress_total=1)
    jobs.claim(job.id)

    class CancelBeforeBackDevice(_FakeU2Device):
        def app_current(self) -> dict[str, object]:
            if (
                self.screen == "share"
                and self._copy_clicked
                and jobs.get(job.id).state is JobState.running
            ):
                jobs.transition(job.id, JobState.cancelled)
            return super().app_current()

    device = CancelBeforeBackDevice()
    result = _adapter(tmp_path, device, jobs=jobs).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1", "job_id": job.id},
            expected_count=1,
        )
    )

    assert result.status == "failed"
    assert result.detail == "cancelled"
    assert ("press", "back") not in device.actions


def test_collection_reports_cancellation_during_selector_wait(
    tmp_path: Path,
) -> None:
    """A selector wait must poll cancellation instead of returning a layout diagnosis."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="shop_collection", input_data={}, progress_total=1)
    jobs.claim(job.id)

    class CancellingExists:
        def __call__(self, *, timeout: float) -> bool:
            _ = timeout
            jobs.transition(job.id, JobState.cancelled)
            return False

    class CancellingSelectorDevice(_FakeU2Device):
        def __call__(self, **query: str) -> _FakeUiObject:
            if next(iter(query.items())) == ("text", "店铺"):
                candidate = _FakeUiObject(False, lambda: None)
                candidate.exists = CancellingExists()  # type: ignore[assignment]
                return candidate
            return super().__call__(**query)

    device = CancellingSelectorDevice()
    result = _adapter(tmp_path, device, jobs=jobs).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1", "job_id": job.id},
            expected_count=1,
        )
    )

    assert result.status == "failed"
    assert result.detail == "cancelled"
    assert not any(action[0] == "click" for action in device.actions)


def test_collection_waits_for_a_changed_clipboard_product_url(tmp_path: Path) -> None:
    """Reading once would preserve the stale pre-Copy link instead of the delayed new one."""
    stale = "https://xhslink.com/stale-product"
    current = "https://xhslink.com/current-product"
    device = _FakeU2Device(
        link=current,
        clipboard_before=stale,
        clipboard_after=[stale, stale, current],
    )

    result = _adapter(
        tmp_path,
        device,
        clipboard_timeout_seconds=0.1,
        transition_poll_interval=0,
    ).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "succeeded"
    assert str(result.items[0].source_url) == current
    assert device.clipboard_reads >= 4


def test_collection_temporarily_uses_input_ime_for_restricted_clipboard(
    tmp_path: Path,
) -> None:
    """Android clipboard restrictions may require InputIME, but the user's IME is restored."""

    class RestrictedClipboardDevice(_FakeU2Device):
        def __init__(self) -> None:
            super().__init__()
            self.input_ime_active = False
            self.clipboard_broadcasts = 0

        def current_ime(self) -> str:
            return "com.vivo.inputmethod/.ImeService"

        def set_input_ime(self, enable: bool = True) -> None:
            assert enable is True
            self.actions.append(("set_input_ime",))
            self.input_ime_active = True

        def shell(self, argv: list[str]) -> SimpleNamespace:
            self.actions.append(("shell", *argv))
            if argv[:2] == ["ime", "set"]:
                self.input_ime_active = False
            elif argv[:2] == ["am", "start"]:
                self.screen = "profile"
            if argv == ["am", "broadcast", "-a", "ADB_KEYBOARD_GET_CLIPBOARD"]:
                self.clipboard_broadcasts += 1
                if self.clipboard_broadcasts == 1:
                    return SimpleNamespace(
                        output="Broadcast completed: result=0"
                    )
                encoded = (
                    "c2hhcmUgaHR0cHM6Ly94aHNsaW5rLmNvbS9wcm9kdWN0LWEgb3Blbg=="
                    if self._copy_clicked
                    else ""
                )
                return SimpleNamespace(
                    output=(
                        'Broadcast completed: result=-1, '
                        f'data="{encoded}"'
                    )
                )
            return SimpleNamespace(output="")

        @property
        def clipboard(self) -> str:
            raise RuntimeError("clipboard SecurityException")

    device = RestrictedClipboardDevice()
    result = _adapter(tmp_path, device).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "succeeded"
    assert result.complete is True
    assert device.clipboard_broadcasts >= 2
    assert device.input_ime_active is False
    assert ("shell", "ime", "set", "com.vivo.inputmethod/.ImeService") in device.actions


def test_collection_rejects_an_unchanged_valid_clipboard_url(tmp_path: Path) -> None:
    """A valid URL already present before Copy is stale evidence, not this card's result."""
    stale = "https://xhslink.com/stale-product"
    device = _FakeU2Device(
        link=stale,
        clipboard_before=stale,
        clipboard_after=[stale],
    )

    result = _adapter(
        tmp_path,
        device,
        clipboard_timeout_seconds=0,
    ).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "needs_human"
    assert result.succeeded_count == 0
    assert result.observed_count == 1
    assert result.rejected_items[0].reason == "stale_clipboard"


def test_partial_shop_collection_reports_explicit_expected_discovered_and_missing(
    tmp_path: Path,
) -> None:
    """One harvested product from an expected two remains a factual 1/2 partial result."""
    device = _FakeU2Device()

    result = _adapter(tmp_path, device).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=2,
        )
    )

    assert result.status == "partial"
    assert result.detail == "expected_products_missing"
    assert result.expected_count == 2
    assert result.observed_count == 1
    assert result.succeeded_count == 1
    assert len(result.missing_items) == 1
    assert result.missing_items[0].reason == "expected_product_not_discovered"
    assert result.complete is False
    assert str(result.items[0].source_url) == "https://xhslink.com/product-a"


def test_same_title_cards_with_distinct_urls_are_both_accounted(tmp_path: Path) -> None:
    """Title equality cannot silently discard a different card and canonical URL."""
    device = _FakeU2Device(
        shop_xml=SAME_TITLE_SHOP_XML,
        links_by_y={150: "https://xhslink.com/product-a", 450: "https://xhslink.com/product-b"},
    )

    result = _adapter(tmp_path, device).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=2,
        )
    )

    assert result.status == "succeeded"
    assert result.observed_count == 2
    assert {str(item.source_url) for item in result.items} == {
        "https://xhslink.com/product-a",
        "https://xhslink.com/product-b",
    }


def test_duplicate_url_card_is_an_explicit_rejected_observation(tmp_path: Path) -> None:
    """A second card resolving to the same URL must remain visible as rejected evidence."""
    duplicate = "https://xhslink.com/product-a"
    device = _FakeU2Device(
        shop_xml=SAME_TITLE_SHOP_XML,
        links_by_y={150: duplicate, 450: duplicate},
    )

    result = _adapter(tmp_path, device).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=2,
        )
    )

    assert result.status == "needs_human"
    assert result.succeeded_count == 1
    assert result.observed_count == 2
    assert len(result.rejected_items) == 1
    assert result.rejected_items[0].reason == "duplicate_source_url"
    assert result.rejected_items[0].raw_evidence["source_url"] == duplicate
    assert len(result.missing_items) == 1
    assert result.missing_items[0].reference == "expected_product:2"


def test_overlapping_viewports_use_unique_urls_for_nn_and_keep_duplicate_evidence(
    tmp_path: Path,
) -> None:
    """Counting the repeated B card in A,B then B,C as overflow would fail a real 3/3."""
    first_viewport = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node text="商品甲完整标题" bounds="[20,120][600,180]" />
  <node content-desc="到手价¥10.00已售10+" bounds="[20,225][600,280]" />
  <node text="商品乙完整标题" bounds="[20,420][600,480]" />
  <node content-desc="到手价¥20.00已售20+" bounds="[20,525][600,580]" />
</hierarchy>"""
    second_viewport = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node text="商品乙完整标题" bounds="[20,120][600,180]" />
  <node content-desc="到手价¥20.00已售20+" bounds="[20,225][600,280]" />
  <node text="商品丙完整标题" bounds="[20,420][600,480]" />
  <node content-desc="到手价¥30.00已售30+" bounds="[20,525][600,580]" />
  <node text="没有更多商品了" bounds="[0,1400][720,1500]" />
</hierarchy>"""
    viewport_links = [
        {
            150: "https://xhslink.com/product-a",
            450: "https://xhslink.com/product-b",
        },
        {
            150: "https://xhslink.com/product-b",
            450: "https://xhslink.com/product-c",
        },
    ]

    class OverlappingViewportDevice(_FakeU2Device):
        def __init__(self) -> None:
            super().__init__(shop_xml=first_viewport)
            self.viewport = 0

        def click(self, x: int, y: int) -> None:
            self.link = viewport_links[self.viewport][y]
            super().click(x, y)

        def swipe(
            self, x1: int, y1: int, x2: int, y2: int, duration: float
        ) -> None:
            super().swipe(x1, y1, x2, y2, duration)
            self.viewport = 1
            self.screens["shop"] = second_viewport

    result = _adapter(tmp_path, OverlappingViewportDevice()).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=3,
        )
    )

    assert result.status == "succeeded"
    assert result.succeeded_count == 3
    assert result.observed_count == 4
    assert result.raw_observation_count == 4
    assert result.duplicate_observation_count == 1
    assert len(result.rejected_items) == 1
    assert result.rejected_items[0].reason == "duplicate_source_url"
    assert result.missing_items == []
    assert result.overflow_count == 0
    assert result.complete is True


def test_collection_uses_the_requested_device_when_multiple_are_connected(
    tmp_path: Path,
) -> None:
    """A request-scoped device ID must select that phone instead of reporting ambiguity."""
    device = _FakeU2Device()
    connected_serials: list[str] = []

    class MultipleClient:
        def device_list(self) -> list[_FakeAdbDevice]:
            return [_FakeAdbDevice("phone-1"), _FakeAdbDevice("phone-2")]

    adapter = AndroidDeviceAdapter(
        runtime_dir=tmp_path / "runtime",
        adb_client_factory=MultipleClient,
        u2_connector=lambda serial: connected_serials.append(serial) or device,
        executable_resolver=lambda _: "C:/Android/platform-tools/adb.exe",
        profile_settle_seconds=0,
        sleep=lambda _: None,
    )

    result = adapter.collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1", "device_id": "phone-2"},
            expected_count=1,
        )
    )

    assert result.status == "succeeded"
    assert connected_serials == ["phone-2"]


def test_concurrent_collection_on_same_serial_reports_device_busy(
    tmp_path: Path,
) -> None:
    """Two workers navigating one serial concurrently would corrupt both evidence trails."""
    entered = Event()
    release = Event()

    class BlockingDevice(_FakeU2Device):
        def open_url(self, url: str) -> None:
            entered.set()
            release.wait(timeout=1)
            super().open_url(url)

    device = BlockingDevice()
    adapter = _adapter(tmp_path, device)
    request = CollectionRequest(
        capability="shop_products",
        parameters={"account_user_id": "account-1"},
        expected_count=1,
    )

    timer = Timer(0.3, release.set)
    timer.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(adapter.collect_shop, request)
            assert entered.wait(timeout=1)
            started_at = time.monotonic()
            second = adapter.collect_shop(request)
            elapsed = time.monotonic() - started_at
            release.set()
            first_result = first.result(timeout=2)
    finally:
        release.set()
        timer.cancel()

    assert first_result.status == "succeeded"
    assert second.status == "needs_human"
    assert second.detail == "device_busy"
    assert elapsed < 0.2


def test_concurrent_collection_on_different_serials_can_both_navigate(
    tmp_path: Path,
) -> None:
    """Replacing per-serial reservations with one global lock would block another phone."""
    release = Event()
    entered = {"phone-1": Event(), "phone-2": Event()}

    class MultipleClient:
        def device_list(self) -> list[_FakeAdbDevice]:
            return [_FakeAdbDevice("phone-1"), _FakeAdbDevice("phone-2")]

    class ParallelDevice(_FakeU2Device):
        def __init__(self, serial: str) -> None:
            super().__init__()
            self.serial = serial

        def open_url(self, url: str) -> None:
            entered[self.serial].set()
            release.wait(timeout=1)
            super().open_url(url)

    devices = {serial: ParallelDevice(serial) for serial in entered}
    adapter = AndroidDeviceAdapter(
        runtime_dir=tmp_path / "runtime",
        adb_client_factory=MultipleClient,
        u2_connector=lambda serial: devices[serial],
        executable_resolver=lambda _: "C:/Android/platform-tools/adb.exe",
        profile_settle_seconds=0,
        selector_timeout_seconds=0,
        sleep=lambda _: None,
    )

    def collect(serial: str) -> CollectionResult:
        return adapter.collect_shop(
            CollectionRequest(
                capability="shop_products",
                parameters={"account_user_id": "account-1", "device_id": serial},
                expected_count=1,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(collect, "phone-1")
        second = executor.submit(collect, "phone-2")
        assert entered["phone-1"].wait(timeout=1)
        assert entered["phone-2"].wait(timeout=1)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert [result.status for result in results] == ["succeeded", "succeeded"]


def test_device_reservation_is_released_when_navigation_raises(tmp_path: Path) -> None:
    """A failed worker must not leave the serial permanently busy."""
    healthy = _FakeU2Device()
    connection_count = 0

    class RaisingDevice(_FakeU2Device):
        def open_url(self, url: str) -> None:
            _ = url
            raise RuntimeError("unexpected navigation failure")

    def connect(_: str) -> _FakeU2Device:
        nonlocal connection_count
        connection_count += 1
        return RaisingDevice() if connection_count == 1 else healthy

    adapter = AndroidDeviceAdapter(
        runtime_dir=tmp_path / "runtime",
        adb_client_factory=_FakeAdbClient,
        u2_connector=connect,
        executable_resolver=lambda _: "C:/Android/platform-tools/adb.exe",
        profile_settle_seconds=0,
        selector_timeout_seconds=0,
        sleep=lambda _: None,
    )
    request = CollectionRequest(
        capability="shop_products",
        parameters={"account_user_id": "account-1"},
        expected_count=1,
    )

    with pytest.raises(RuntimeError, match="unexpected navigation failure"):
        adapter.collect_shop(request)
    retried = adapter.collect_shop(request)

    assert retried.status == "succeeded"
    assert retried.detail is None


def test_return_to_shop_captures_each_bounded_back_transition(tmp_path: Path) -> None:
    """Closing a share panel and detail page may take two backs; both states stay observable."""
    device = _FakeU2Device(back_screens=["detail", "shop"])

    result = _adapter(tmp_path, device).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "succeeded"
    assert [action for action in device.actions if action[0] == "press"] == [
        ("press", "back"),
        ("press", "back"),
    ]


def test_return_to_shop_waits_past_stale_detail_before_another_back(
    tmp_path: Path,
) -> None:
    """A delayed shop render after the second Back must not cause an unsafe third Back."""

    class DelayedReturnDevice(_FakeU2Device):
        def __init__(self) -> None:
            super().__init__(back_screens=[])
            self.back_count = 0
            self.return_dumps = 0

        def press(self, key: str) -> None:
            self.actions.append(("press", key))
            self.back_count += 1
            if self.back_count == 1:
                self.screen = "detail"
            elif self.back_count == 2:
                self.return_dumps = 0
                self.screen = "returning"
                self.screens["returning"] = DETAIL_XML
            else:
                self.screen = "initial"

        def dump_hierarchy(self, *, compressed: bool = False) -> str:
            if self.screen == "returning":
                self.return_dumps += 1
                if self.return_dumps >= 2:
                    self.screen = "shop"
            return super().dump_hierarchy(compressed=compressed)

    device = DelayedReturnDevice()

    result = _adapter(
        tmp_path,
        device,
        transition_poll_interval=0,
    ).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1"},
            expected_count=1,
        )
    )

    assert result.status == "succeeded"
    assert [action for action in device.actions if action[0] == "press"] == [
        ("press", "back"),
        ("press", "back"),
    ]


def test_each_screen_transition_persists_contained_screenshot_and_hierarchy(
    tmp_path: Path,
) -> None:
    """Every traversed profile/shop/detail/share/return screen gets both evidence forms."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir)
    job = jobs.create(job_type="shop_collection", input_data={}, progress_total=1)
    jobs.claim(job.id)
    device = _FakeU2Device()

    result = _adapter(tmp_path, device, jobs=jobs).collect_shop(
        CollectionRequest(
            capability="shop_products",
            parameters={"account_user_id": "account-1", "job_id": job.id},
            expected_count=1,
        )
    )

    assert result.status == "succeeded"
    assert result.complete is True
    assert len(result.evidence_artifacts) == 10
    persisted = jobs.get(job.id).artifacts
    assert [artifact.kind for artifact in persisted] == [
        kind
        for _ in range(5)
        for kind in ("android_screenshot", "android_ui_hierarchy")
    ]
    for artifact in persisted:
        absolute = (runtime_dir / artifact.path).resolve()
        absolute.relative_to(runtime_dir.resolve())
        assert absolute.is_file()


class _StaticDeviceAdapter:
    selector_profile_version = DEFAULT_SELECTOR_PROFILE_VERSION

    def __init__(self) -> None:
        self.requests: list[CollectionRequest] = []

    def health(self) -> DeviceHealth:
        return DeviceHealth(
            status="available",
            device_id="phone-1",
            detail="ready",
            raw_evidence={"selector_profile_version": self.selector_profile_version},
        )

    def collect_shop(self, request: CollectionRequest) -> CollectionResult:
        self.requests.append(request)
        return CollectionResult(
            status="succeeded",
            evidence_artifacts=[],
            items=[
                CollectionItem(
                    id="product-a",
                    kind="shop_product",
                    source_url="https://xhslink.com/product-a",
                    raw_evidence={"title": "商品 A"},
                )
            ],
            expected_count_known=True,
            expected_count=1,
            succeeded_count=1,
            observed_count=1,
            missing_items=[],
            overflow_count=0,
            complete=True,
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_device_and_shop_collection_apis_return_database_backed_counts(
    tmp_path: Path,
) -> None:
    """The public API queues work and the job preserves URL counts pending deep evidence."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "workbench.sqlite3",
        )
    )
    adapter = _StaticDeviceAdapter()
    app.state.android_adapter = adapter
    app.state.shop_service = ShopCollectionService(
        job_service=app.state.job_service,
        device_adapter=adapter,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        devices = await client.get("/api/v1/devices")
        collected = await client.post(
            "/api/v1/shop-collections",
            json={
                "account_user_id": "account-1",
                "account_name": "账号甲",
                "expected_count": 1,
            },
        )

    assert devices.status_code == 200
    assert devices.json()[0]["detail"] == "ready"
    assert collected.status_code == 202
    payload = collected.json()
    assert payload["status"] == "queued"
    for _ in range(100):
        job = app.state.job_service.get(payload["job_id"])
        if job.state not in {JobState.queued, JobState.running}:
            break
        time.sleep(0.01)
    assert job.state is JobState.needs_human
    assert job.error_category == "product_evidence_verification_pending"
    persisted = next(
        artifact.metadata["result"]
        for artifact in job.artifacts
        if artifact.kind == "shop_collection_result"
    )
    assert persisted["expected_count"] == 1
    assert persisted["discovered_count"] == 1
    assert persisted["collected_count"] == 1
    assert persisted["succeeded_count"] == 0
    assert persisted["missing_count"] == 1
    assert persisted["rejected_count"] == 0
    assert persisted["complete"] is False
    assert job.progress_current == 0
    assert adapter.requests[0].parameters["job_id"] == payload["job_id"]
    app.state.shop_service.close()
