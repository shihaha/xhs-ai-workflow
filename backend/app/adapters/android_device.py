"""Evidence-backed ADB/uiautomator2 adapter for controlled XHS shop collection."""

from __future__ import annotations

import base64
import importlib.metadata
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from shutil import which
from threading import Lock, local
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    DeviceHealth,
    MissingCollectionItem,
    RejectedCollectionItem,
)
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


DEFAULT_SELECTOR_PROFILE_VERSION = "xhs-android-2026-08-v1"
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,500}")
_PRICE_SOLD = re.compile(r"^(?:到手价)?(¥[\d.]+)已售([\d.万+]+)")
_IMAGE_SCREENSHOT_KIND = "android_screenshot"
_HIERARCHY_KIND = "android_ui_hierarchy"
_ZERO_WIDTH = "\u200b\u200c\u200d\ufeff"
_DEVICE_RESERVATIONS_GUARD = Lock()
_DEVICE_RESERVATIONS: dict[str, Lock] = {}
_BAD_TITLE_PREFIXES = (
    "限时立减",
    "立减",
    "到手价",
    "当月",
    "店铺通用",
    "无门槛",
    "综合",
    "销量",
    "新品",
    "价格",
    "已领",
    "首页",
    "上新",
    "客服",
    "好评",
    "极速",
    "粉丝",
    "已售",
    "昨天",
    "加分",
    "搜索店铺",
    "搜索",
)


@dataclass(frozen=True)
class SelectorQuery:
    attribute: str
    value: str


@dataclass(frozen=True)
class AndroidSelectorProfile:
    version: str
    package_name: str
    shop_entry: tuple[SelectorQuery, ...]
    share_product: tuple[SelectorQuery, ...]
    copy_link: tuple[SelectorQuery, ...]
    login_markers: tuple[str, ...]
    captcha_markers: tuple[str, ...]
    shop_markers: tuple[str, ...]
    detail_markers: tuple[str, ...]
    end_markers: tuple[str, ...]
    profile_activity_markers: tuple[str, ...]
    shop_activity_markers: tuple[str, ...]
    detail_activity_markers: tuple[str, ...]


ANDROID_SELECTOR_PROFILES: dict[str, AndroidSelectorProfile] = {
    DEFAULT_SELECTOR_PROFILE_VERSION: AndroidSelectorProfile(
        version=DEFAULT_SELECTOR_PROFILE_VERSION,
        package_name="com.xingin.xhs",
        shop_entry=(SelectorQuery("text", "店铺"),),
        share_product=(SelectorQuery("description", "分享商品"),),
        copy_link=(SelectorQuery("description", "复制链接"),),
        login_markers=("手机号登录", "验证码登录", "登录后查看", "登录/注册"),
        captcha_markers=("请完成安全验证", "滑块验证", "安全验证", "完成验证"),
        shop_markers=("到手价", "已售", "没有更多商品了"),
        detail_markers=("立即购买", "加入购物车"),
        end_markers=("没有更多商品了",),
        profile_activity_markers=("NewOtherUser",),
        shop_activity_markers=("ShopDetail",),
        detail_activity_markers=("GoodsDetail",),
    )
}


@dataclass(frozen=True)
class ShopProductPosition:
    title: str
    price: str
    sold: str
    rank: str
    discount: str
    center_x: int
    center_y: int


@dataclass(frozen=True)
class _ScreenEvidence:
    transition: str
    hierarchy: str
    screenshot_sha256: str
    artifacts: tuple[str, ...]

    def raw(self) -> dict[str, Any]:
        return {
            "transition": self.transition,
            "hierarchy": self.hierarchy,
            "screenshot_sha256": self.screenshot_sha256,
            "artifacts": list(self.artifacts),
        }


@dataclass(frozen=True)
class _ConnectedDevice:
    health: DeviceHealth
    device: Any | None


class _DeviceDisconnected(RuntimeError):
    pass


def parse_shop_hierarchy(xml: str) -> list[ShopProductPosition]:
    """Port the tutorial title/price/position rules to a pure XML parser."""
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, TypeError) as error:
        raise ValueError("invalid_ui_hierarchy") from error

    nodes: list[dict[str, Any]] = []
    for element in root.iter("node"):
        text = _clean_text(element.attrib.get("text", ""))
        description = _clean_text(
            element.attrib.get("content-desc", element.attrib.get("desc", ""))
        )
        bounds = _bounds(element.attrib.get("bounds", ""))
        nodes.append({"text": text, "description": description, "bounds": bounds})

    products: list[ShopProductPosition] = []
    seen: set[tuple[str, int, int]] = set()
    for index, node in enumerate(nodes):
        price_text = node["description"] or node["text"]
        price_match = _PRICE_SOLD.match(price_text)
        if price_match is None:
            continue
        rank = ""
        discount = ""
        title_candidates: list[dict[str, Any]] = []
        for previous_index in range(index - 1, max(-1, index - 30), -1):
            previous = nodes[previous_index]
            previous_price = previous["description"] or previous["text"]
            if _PRICE_SOLD.match(previous_price):
                break
            candidate = previous["text"]
            if candidate.startswith("当月") and "名" in candidate:
                rank = candidate
                continue
            if candidate.startswith(("限时立减", "立减")):
                discount = candidate
                continue
            if (
                not candidate
                or previous["bounds"] is None
                or len(candidate) < 5
                or candidate.startswith(_BAD_TITLE_PREFIXES)
            ):
                continue
            title_candidates.append(previous)
        if not title_candidates:
            continue
        title_node = max(title_candidates, key=lambda item: len(item["text"]))
        x1, y1, x2, y2 = title_node["bounds"]
        key = (title_node["text"], (x1 + x2) // 2, (y1 + y2) // 2)
        if key in seen:
            continue
        seen.add(key)
        products.append(
            ShopProductPosition(
                title=title_node["text"][:500],
                price=price_match.group(1),
                sold=price_match.group(2),
                rank=rank,
                discount=discount,
                center_x=key[1],
                center_y=key[2],
            )
        )
    return products


class AndroidDeviceAdapter:
    """Collect XHS product links without bypassing login or challenge screens."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        job_service: JobService | None = None,
        adb_executable: str = "adb",
        device_id: str | None = None,
        selector_profile_version: str = DEFAULT_SELECTOR_PROFILE_VERSION,
        adb_client_factory: Callable[[], Any] | None = None,
        u2_connector: Callable[[str], Any] | None = None,
        executable_resolver: Callable[[str], str | None] = which,
        max_shop_screens: int = 10,
        profile_settle_seconds: float = 1,
        selector_timeout_seconds: float = 5,
        transition_timeout_seconds: float = 8,
        transition_poll_interval: float = 0.25,
        clipboard_timeout_seconds: float = 5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if selector_profile_version not in ANDROID_SELECTOR_PROFILES:
            raise ValueError(f"Unknown Android selector profile: {selector_profile_version}")
        self.runtime_dir = runtime_dir.resolve()
        self.job_service = job_service
        self.adb_executable = adb_executable
        self.device_id = device_id
        self.selector_profile_version = selector_profile_version
        self.profile = ANDROID_SELECTOR_PROFILES[selector_profile_version]
        self._adb_client_factory = adb_client_factory or _default_adb_client
        self._u2_connector = u2_connector or _default_u2_connect
        self._executable_resolver = executable_resolver
        self.max_shop_screens = max(1, max_shop_screens)
        self.profile_settle_seconds = max(profile_settle_seconds, 0)
        self.selector_timeout_seconds = max(selector_timeout_seconds, 0)
        self.transition_timeout_seconds = max(transition_timeout_seconds, 0)
        self.transition_poll_interval = max(transition_poll_interval, 0)
        self.clipboard_timeout_seconds = max(clipboard_timeout_seconds, 0)
        self._sleep = sleep
        self._monotonic = monotonic
        self._cancellation_context = local()

    def health(self) -> DeviceHealth:
        return self._connect(self.device_id).health

    def collect_shop(
        self,
        request: CollectionRequest,
        *,
        _connected: _ConnectedDevice | None = None,
        _reservation_held: bool = False,
    ) -> CollectionResult:
        callback = request.parameters.get("is_cancelled")
        previous_callback = getattr(
            self._cancellation_context, "callback", None
        )
        self._cancellation_context.callback = callback if callable(callback) else None
        try:
            return self._collect_shop(
                request,
                _connected=_connected,
                _reservation_held=_reservation_held,
            )
        finally:
            self._cancellation_context.callback = previous_callback

    def _collect_shop(
        self,
        request: CollectionRequest,
        *,
        _connected: _ConnectedDevice | None = None,
        _reservation_held: bool = False,
    ) -> CollectionResult:
        expected = request.expected_count
        job_id = self._validated_job_id(request)
        requested_profile = request.parameters.get(
            "selector_profile_version", self.selector_profile_version
        )
        if requested_profile != self.selector_profile_version:
            return self._result(
                request=request,
                status="needs_human",
                detail="selector_profile_unknown",
                items=[],
                rejected_items=[],
                artifacts=[],
                transitions=[],
            )
        if self._is_cancelled(job_id):
            return self._result(
                request=request,
                status="failed",
                detail="cancelled",
                items=[],
                rejected_items=[],
                artifacts=[],
                transitions=[],
            )

        account_user_id = _safe_token(request.parameters.get("account_user_id"))
        if account_user_id is None:
            return self._result(
                request=request,
                status="needs_human",
                detail="invalid_account_user_id",
                items=[],
                rejected_items=[],
                artifacts=[],
                transitions=[],
            )

        requested_device_id = request.parameters.get("device_id")
        if requested_device_id is None:
            selected_device_id = self.device_id
        elif isinstance(requested_device_id, str) and requested_device_id.strip():
            selected_device_id = requested_device_id.strip()
        else:
            return self._result(
                request=request,
                status="needs_human",
                detail="invalid_device_id",
                items=[],
                rejected_items=[],
                artifacts=[],
                transitions=[],
            )

        connected = _connected or self._connect(selected_device_id)
        if connected.device is None:
            status: Literal["needs_human", "failed"] = (
                "needs_human"
                if connected.health.status == "needs_human"
                else "failed"
            )
            return self._result(
                request=request,
                status=status,
                detail=connected.health.detail,
                items=[],
                rejected_items=[],
                artifacts=[],
                transitions=[{"device_health": connected.health.model_dump(mode="json")}],
            )

        if not _reservation_held:
            serial = connected.health.device_id
            assert serial is not None
            reservation = _device_reservation(serial)
            if not reservation.acquire(blocking=False):
                return self._result(
                    request=request,
                    status="needs_human",
                    detail="device_busy",
                    items=[],
                    rejected_items=[],
                    artifacts=[],
                    transitions=[
                        {"device_health": connected.health.model_dump(mode="json")}
                    ],
                )
            try:
                return self.collect_shop(
                    request,
                    _connected=connected,
                    _reservation_held=True,
                )
            finally:
                reservation.release()

        device = connected.device
        items: list[CollectionItem] = []
        rejected_items: list[RejectedCollectionItem] = []
        artifact_paths: list[str] = []
        transitions: list[dict[str, Any]] = []
        sequence = 0

        def capture(
            name: str, ready: Callable[[str], bool] | None = None
        ) -> _ScreenEvidence:
            nonlocal sequence
            sequence += 1
            evidence = self._capture_transition(
                device, job_id, name, sequence, ready=ready
            )
            artifact_paths.extend(evidence.artifacts)
            transitions.append(evidence.raw())
            return evidence

        try:
            self._open_profile(device, account_user_id)
            if self._wait_for_duration_or_cancellation(
                job_id, self.profile_settle_seconds
            ):
                cancelled = self._cancelled_result(
                    request,
                    job_id,
                    items,
                    rejected_items,
                    artifact_paths,
                    transitions,
                )
                assert cancelled is not None
                return cancelled
            profile_screen = capture(
                "account_profile",
                ready=lambda hierarchy: (
                    self._blocked_reason(hierarchy) is not None
                    or self._is_profile_activity(device)
                ),
            )
            cancelled = self._cancelled_result(
                request, job_id, items, rejected_items, artifact_paths, transitions
            )
            if cancelled is not None:
                return cancelled
            blocked = self._blocked_reason(profile_screen.hierarchy)
            if blocked is not None:
                return self._result(
                    request=request,
                    status="needs_human",
                    detail=blocked,
                    items=items,
                    rejected_items=rejected_items,
                    artifacts=artifact_paths,
                    transitions=transitions,
                )
            click_outcome = self._click_selector(
                device, self.profile.shop_entry, job_id
            )
            if click_outcome == "cancelled":
                return self._cancelled_result(
                    request,
                    job_id,
                    items,
                    rejected_items,
                    artifact_paths,
                    transitions,
                ) or self._result(
                    request=request,
                    status="failed",
                    detail="cancelled",
                    items=items,
                    rejected_items=rejected_items,
                    artifacts=artifact_paths,
                    transitions=transitions,
                )
            if click_outcome != "clicked":
                return self._result(
                    request=request,
                    status="needs_human",
                    detail="selector_changed",
                    items=items,
                    rejected_items=rejected_items,
                    artifacts=artifact_paths,
                    transitions=transitions,
                )

            shop_screen = capture(
                "shop_page",
                ready=lambda hierarchy: (
                    self._blocked_reason(hierarchy) is not None
                    or self._is_shop_hierarchy(hierarchy)
                ),
            )
            cancelled = self._cancelled_result(
                request, job_id, items, rejected_items, artifact_paths, transitions
            )
            if cancelled is not None:
                return cancelled
            blocked = self._blocked_reason(shop_screen.hierarchy)
            if blocked is not None:
                return self._result(
                    request=request,
                    status="needs_human",
                    detail=blocked,
                    items=items,
                    rejected_items=rejected_items,
                    artifacts=artifact_paths,
                    transitions=transitions,
                )

            visited_card_positions: set[tuple[str, int, int]] = set()
            seen_urls: set[str] = set()
            observation_count = 0
            for screen_index in range(self.max_shop_screens):
                cancelled = self._cancelled_result(
                    request, job_id, items, rejected_items, artifact_paths, transitions
                )
                if cancelled is not None:
                    return cancelled
                try:
                    products = parse_shop_hierarchy(shop_screen.hierarchy)
                except ValueError:
                    return self._result(
                        request=request,
                        status="needs_human",
                        detail="selector_changed",
                        items=items,
                        rejected_items=rejected_items,
                        artifacts=artifact_paths,
                        transitions=transitions,
                    )
                if not products and not self._is_end(shop_screen.hierarchy):
                    return self._result(
                        request=request,
                        status="needs_human",
                        detail="selector_changed",
                        items=items,
                        rejected_items=rejected_items,
                        artifacts=artifact_paths,
                        transitions=transitions,
                    )

                screen_fingerprint = sha256(
                    shop_screen.hierarchy.encode("utf-8")
                ).hexdigest()
                for product in products:
                    traversal_key = (
                        screen_fingerprint,
                        product.center_x,
                        product.center_y,
                    )
                    if traversal_key in visited_card_positions:
                        continue
                    visited_card_positions.add(traversal_key)
                    observation_count += 1
                    observation_reference = (
                        f"shop_card:{screen_index + 1}:{product.center_x}:"
                        f"{product.center_y}:{observation_count}"
                    )
                    cancelled = self._cancelled_result(
                        request,
                        job_id,
                        items,
                        rejected_items,
                        artifact_paths,
                        transitions,
                    )
                    if cancelled is not None:
                        return cancelled
                    device.click(product.center_x, product.center_y)
                    detail_screen = capture(
                        f"product_{observation_count}_detail",
                        ready=lambda hierarchy: (
                            self._blocked_reason(hierarchy) is not None
                            or self._is_detail_hierarchy(hierarchy)
                        ),
                    )
                    cancelled = self._cancelled_result(
                        request,
                        job_id,
                        items,
                        rejected_items,
                        artifact_paths,
                        transitions,
                    )
                    if cancelled is not None:
                        return cancelled
                    blocked = self._blocked_reason(detail_screen.hierarchy)
                    if blocked is not None:
                        rejected_items.append(
                            _rejected_product(
                                product,
                                blocked,
                                detail_screen.raw(),
                                reference=observation_reference,
                            )
                        )
                        return self._result(
                            request=request,
                            status="needs_human",
                            detail=blocked,
                            items=items,
                            rejected_items=rejected_items,
                            artifacts=artifact_paths,
                            transitions=transitions,
                        )
                    if not self._is_detail(device, detail_screen.hierarchy):
                        rejected_items.append(
                            _rejected_product(
                                product,
                                "selector_changed",
                                detail_screen.raw(),
                                reference=observation_reference,
                            )
                        )
                        return self._result(
                            request=request,
                            status="needs_human",
                            detail="selector_changed",
                            items=items,
                            rejected_items=rejected_items,
                            artifacts=artifact_paths,
                            transitions=transitions,
                        )
                    click_outcome = self._click_selector(
                        device, self.profile.share_product, job_id
                    )
                    if click_outcome == "cancelled":
                        return self._cancelled_result(
                            request,
                            job_id,
                            items,
                            rejected_items,
                            artifact_paths,
                            transitions,
                        ) or self._result(
                            request=request,
                            status="failed",
                            detail="cancelled",
                            items=items,
                            rejected_items=rejected_items,
                            artifacts=artifact_paths,
                            transitions=transitions,
                        )
                    if click_outcome != "clicked":
                        rejected_items.append(
                            _rejected_product(
                                product,
                                "selector_changed",
                                detail_screen.raw(),
                                reference=observation_reference,
                            )
                        )
                        return self._result(
                            request=request,
                            status="needs_human",
                            detail="selector_changed",
                            items=items,
                            rejected_items=rejected_items,
                            artifacts=artifact_paths,
                            transitions=transitions,
                        )
                    share_screen = capture(
                        f"product_{observation_count}_share",
                        ready=lambda hierarchy: (
                            self._blocked_reason(hierarchy) is not None
                            or self._is_share_hierarchy(hierarchy)
                        ),
                    )
                    cancelled = self._cancelled_result(
                        request,
                        job_id,
                        items,
                        rejected_items,
                        artifact_paths,
                        transitions,
                    )
                    if cancelled is not None:
                        return cancelled
                    blocked = self._blocked_reason(share_screen.hierarchy)
                    if blocked is not None:
                        rejected_items.append(
                            _rejected_product(
                                product,
                                blocked,
                                share_screen.raw(),
                                reference=observation_reference,
                            )
                        )
                        return self._result(
                            request=request,
                            status="needs_human",
                            detail=blocked,
                            items=items,
                            rejected_items=rejected_items,
                            artifacts=artifact_paths,
                            transitions=transitions,
                        )
                    cancelled = self._cancelled_result(
                        request,
                        job_id,
                        items,
                        rejected_items,
                        artifact_paths,
                        transitions,
                    )
                    if cancelled is not None:
                        return cancelled
                    previous_clipboard = _read_clipboard(device)
                    click_outcome = self._click_selector(
                        device, self.profile.copy_link, job_id
                    )
                    if click_outcome == "cancelled":
                        cancelled = self._cancelled_result(
                            request,
                            job_id,
                            items,
                            rejected_items,
                            artifact_paths,
                            transitions,
                        )
                        assert cancelled is not None
                        return cancelled
                    link: str | None = None
                    link_failure = "product_link_unavailable"
                    if click_outcome == "clicked":
                        link, link_failure = self._fresh_clipboard_product_url(
                            device,
                            previous_clipboard=previous_clipboard,
                            job_id=job_id,
                        )
                    cancelled = self._cancelled_result(
                        request,
                        job_id,
                        items,
                        rejected_items,
                        artifact_paths,
                        transitions,
                    )
                    if cancelled is not None:
                        return cancelled
                    product_evidence = {
                        "title": product.title,
                        "price": product.price,
                        "sold": product.sold,
                        "rank": product.rank,
                        "discount": product.discount,
                        "detail_screen": detail_screen.raw(),
                        "share_screen": share_screen.raw(),
                    }
                    if link is None:
                        rejected_items.append(
                            _rejected_product(
                                product,
                                link_failure,
                                product_evidence,
                                reference=observation_reference,
                            )
                        )
                    elif link in seen_urls:
                        product_evidence["source_url"] = link
                        rejected_items.append(
                            _rejected_product(
                                product,
                                "duplicate_source_url",
                                product_evidence,
                                reference=observation_reference,
                            )
                        )
                    else:
                        seen_urls.add(link)
                        items.append(
                            CollectionItem(
                                id=sha256(link.encode("utf-8")).hexdigest(),
                                kind="shop_product",
                                source_url=link,
                                raw_evidence=product_evidence,
                                data={
                                    "title": product.title,
                                    "price": product.price,
                                    "sold": product.sold,
                                    "rank": product.rank,
                                    "discount": product.discount,
                                },
                            )
                        )

                    returned_to_shop = False
                    for back_attempt in range(1, 6):
                        cancelled = self._cancelled_result(
                            request,
                            job_id,
                            items,
                            rejected_items,
                            artifact_paths,
                            transitions,
                        )
                        if cancelled is not None:
                            return cancelled
                        if self._is_shop_activity(device):
                            returned_to_shop = True
                            break
                        cancelled = self._cancelled_result(
                            request,
                            job_id,
                            items,
                            rejected_items,
                            artifact_paths,
                            transitions,
                        )
                        if cancelled is not None:
                            return cancelled
                        device.press("back")
                        shop_screen = capture(
                            f"product_{observation_count}_shop_return_{back_attempt}",
                            ready=lambda hierarchy, allow_detail=back_attempt == 1: (
                                self._blocked_reason(hierarchy) is not None
                                or self._is_shop(device, hierarchy)
                                or (
                                    allow_detail
                                    and self._is_detail_hierarchy(hierarchy)
                                    and not self._is_share_hierarchy(hierarchy)
                                )
                            ),
                        )
                        cancelled = self._cancelled_result(
                            request,
                            job_id,
                            items,
                            rejected_items,
                            artifact_paths,
                            transitions,
                        )
                        if cancelled is not None:
                            return cancelled
                        blocked = self._blocked_reason(shop_screen.hierarchy)
                        if blocked is not None:
                            return self._result(
                                request=request,
                                status="needs_human",
                                detail=blocked,
                                items=items,
                                rejected_items=rejected_items,
                                artifacts=artifact_paths,
                                transitions=transitions,
                            )
                        if self._is_shop(device, shop_screen.hierarchy):
                            returned_to_shop = True
                            break
                    if not returned_to_shop:
                        return self._result(
                            request=request,
                            status="needs_human",
                            detail="selector_changed",
                            items=items,
                            rejected_items=rejected_items,
                            artifacts=artifact_paths,
                            transitions=transitions,
                        )

                if self._is_end(shop_screen.hierarchy):
                    break
                if expected is not None and len(items) >= expected:
                    break
                cancelled = self._cancelled_result(
                    request,
                    job_id,
                    items,
                    rejected_items,
                    artifact_paths,
                    transitions,
                )
                if cancelled is not None:
                    return cancelled
                previous_hierarchy = shop_screen.hierarchy
                device.swipe(360, 1300, 360, 500, 0.6)
                shop_screen = capture(
                    f"shop_scroll_{screen_index + 1}",
                    ready=lambda hierarchy: (
                        self._blocked_reason(hierarchy) is not None
                        or self._is_end(hierarchy)
                        or hierarchy != previous_hierarchy
                    ),
                )
                cancelled = self._cancelled_result(
                    request,
                    job_id,
                    items,
                    rejected_items,
                    artifact_paths,
                    transitions,
                )
                if cancelled is not None:
                    return cancelled

        except Exception as error:
            if not _is_disconnect_error(error):
                raise
            transitions.append(
                {
                    "device_error": {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                }
            )
            return self._result(
                request=request,
                status="failed",
                detail="device_disconnected",
                items=items,
                rejected_items=rejected_items,
                artifacts=artifact_paths,
                transitions=transitions,
            )

        cancelled = self._cancelled_result(
            request, job_id, items, rejected_items, artifact_paths, transitions
        )
        if cancelled is not None:
            return cancelled
        if expected is None:
            return self._result(
                request=request,
                status="needs_human",
                detail="expected_count_unknown",
                items=items,
                rejected_items=rejected_items,
                artifacts=artifact_paths,
                transitions=transitions,
            )
        raw_observed = len(items) + len(rejected_items)
        duplicate_observations = sum(
            item.reason == "duplicate_source_url" for item in rejected_items
        )
        identity_observed = raw_observed - duplicate_observations
        non_duplicate_rejections = len(rejected_items) - duplicate_observations
        if identity_observed > expected:
            status = "failed"
            detail = "discovered_count_exceeds_expected"
        elif non_duplicate_rejections:
            status = "needs_human"
            detail = "product_evidence_unavailable"
        elif identity_observed < expected:
            if rejected_items:
                status = "needs_human"
                detail = "product_evidence_unavailable"
            else:
                status = "partial"
                detail = "expected_products_missing"
        else:
            status = "succeeded"
            detail = None
        return self._result(
            request=request,
            status=status,
            detail=detail,
            items=items,
            rejected_items=rejected_items,
            artifacts=artifact_paths,
            transitions=transitions,
        )

    def _connect(self, selected_device_id: str | None) -> _ConnectedDevice:
        base_evidence: dict[str, Any] = {
            "adb_executable": self.adb_executable,
            "selector_profile_version": self.selector_profile_version,
            "dependency_versions": _dependency_versions(),
        }
        try:
            resolved_adb = self._executable_resolver(self.adb_executable)
        except Exception as error:
            base_evidence["adb_error"] = _error_evidence(error)
            resolved_adb = None
        if resolved_adb is None:
            base_evidence["devices"] = []
            return _ConnectedDevice(
                health=DeviceHealth(
                    status="unavailable",
                    device_id=selected_device_id,
                    detail="adb_unavailable",
                    raw_evidence=base_evidence,
                ),
                device=None,
            )
        base_evidence["resolved_adb"] = str(resolved_adb)
        try:
            adb_client = self._adb_client_factory()
            raw_devices = list(adb_client.device_list())
        except Exception as error:
            base_evidence["devices"] = []
            base_evidence["adb_error"] = _error_evidence(error)
            return _ConnectedDevice(
                health=DeviceHealth(
                    status="unavailable",
                    device_id=selected_device_id,
                    detail=(
                        "device_disconnected"
                        if _is_disconnect_error(error)
                        else "android_dependencies_unavailable"
                    ),
                    raw_evidence=base_evidence,
                ),
                device=None,
            )

        inventory = [_device_record(device) for device in raw_devices]
        base_evidence["devices"] = inventory
        selected = _select_device(raw_devices, selected_device_id)
        if selected is None:
            detail = (
                "multiple_devices"
                if len(raw_devices) > 1 and selected_device_id is None
                else "device_disconnected"
            )
            status: Literal["unavailable", "needs_human"] = (
                "needs_human" if detail == "multiple_devices" else "unavailable"
            )
            return _ConnectedDevice(
                health=DeviceHealth(
                    status=status,
                    device_id=selected_device_id,
                    detail=detail,
                    raw_evidence=base_evidence,
                ),
                device=None,
            )
        selected_record = _device_record(selected)
        serial = selected_record["device_id"]
        if selected_record["state"] != "device":
            return _ConnectedDevice(
                health=DeviceHealth(
                    status="unavailable",
                    device_id=serial,
                    detail="device_disconnected",
                    raw_evidence=base_evidence,
                ),
                device=None,
            )
        try:
            device = self._u2_connector(serial)
            foreground = dict(device.app_current())
        except Exception as error:
            base_evidence["uiautomator_error"] = _error_evidence(error)
            return _ConnectedDevice(
                health=DeviceHealth(
                    status="unavailable",
                    device_id=serial,
                    detail=(
                        "device_disconnected"
                        if _is_disconnect_error(error)
                        else "uiautomator_unavailable"
                    ),
                    raw_evidence=base_evidence,
                ),
                device=None,
            )
        base_evidence["foreground"] = foreground
        base_evidence["expected_package"] = self.profile.package_name
        if foreground.get("package") != self.profile.package_name:
            return _ConnectedDevice(
                health=DeviceHealth(
                    status="needs_human",
                    device_id=serial,
                    detail="wrong_foreground_app",
                    raw_evidence=base_evidence,
                ),
                device=None,
            )
        return _ConnectedDevice(
            health=DeviceHealth(
                status="available",
                device_id=serial,
                detail="ready",
                raw_evidence=base_evidence,
            ),
            device=device,
        )

    def _validated_job_id(self, request: CollectionRequest) -> str | None:
        raw_job_id = request.parameters.get("job_id")
        if raw_job_id is None:
            return None
        if self.job_service is None:
            raise RuntimeError("job_service is required when a shop job_id is supplied.")
        if not isinstance(raw_job_id, str):
            raise ValueError("shop job_id must be a canonical UUID string.")
        try:
            parsed = UUID(raw_job_id)
        except (ValueError, AttributeError) as error:
            raise ValueError("shop job_id must be a canonical UUID string.") from error
        if parsed.version != 4 or str(parsed) != raw_job_id:
            raise ValueError("shop job_id must be a canonical UUID string.")
        state = self.job_service.get(raw_job_id).state
        if state not in {JobState.running, JobState.cancelled}:
            raise ValueError("shop collection job must be running or cancelled.")
        return raw_job_id

    def _capture_transition(
        self,
        device: Any,
        job_id: str | None,
        transition: str,
        sequence: int,
        *,
        ready: Callable[[str], bool] | None = None,
    ) -> _ScreenEvidence:
        try:
            deadline = self._monotonic() + self.transition_timeout_seconds
            hierarchy = device.dump_hierarchy(compressed=False)
            while True:
                if (
                    ready is None
                    or ready(hierarchy)
                    or self._is_cancelled(job_id)
                    or self._monotonic() >= deadline
                ):
                    break
                self._sleep(self.transition_poll_interval)
                if self._is_cancelled(job_id):
                    break
                hierarchy = device.dump_hierarchy(compressed=False)
            screenshot = _screenshot_bytes(device)
        except Exception as error:
            if _is_disconnect_error(error):
                raise _DeviceDisconnected(str(error)) from error
            raise
        if not isinstance(hierarchy, str) or not hierarchy.strip():
            raise ValueError("UI hierarchy was empty.")
        digest = sha256(screenshot).hexdigest()
        if job_id is None:
            return _ScreenEvidence(transition, hierarchy, digest, ())

        assert self.job_service is not None
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", transition).strip("-") or "screen"
        stem = f"{sequence:03d}-{slug}-{uuid4().hex}"
        relative_dir = Path("evidence") / "android" / job_id
        screenshot_relative = relative_dir / f"{stem}.png"
        hierarchy_relative = relative_dir / f"{stem}.xml"
        screenshot_path = self._contained_path(screenshot_relative)
        hierarchy_path = self._contained_path(hierarchy_relative)
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(screenshot)
        hierarchy_path.write_text(hierarchy, encoding="utf-8")
        screenshot_artifact = self.job_service.attach_artifact(
            job_id,
            kind=_IMAGE_SCREENSHOT_KIND,
            path=screenshot_relative.as_posix(),
            metadata={
                "transition": transition,
                "selector_profile_version": self.selector_profile_version,
                "sha256": digest,
            },
        )
        hierarchy_artifact = self.job_service.attach_artifact(
            job_id,
            kind=_HIERARCHY_KIND,
            path=hierarchy_relative.as_posix(),
            metadata={
                "transition": transition,
                "selector_profile_version": self.selector_profile_version,
                "sha256": sha256(hierarchy.encode("utf-8")).hexdigest(),
            },
        )
        return _ScreenEvidence(
            transition,
            hierarchy,
            digest,
            (screenshot_artifact.path, hierarchy_artifact.path),
        )

    def _contained_path(self, relative_path: Path) -> Path:
        candidate = (self.runtime_dir / relative_path).resolve()
        try:
            candidate.relative_to(self.runtime_dir)
        except ValueError as error:
            raise ValueError("Android evidence path escapes runtime storage.") from error
        return candidate

    def _blocked_reason(self, hierarchy: str) -> str | None:
        if any(marker in hierarchy for marker in self.profile.captcha_markers):
            return "captcha_required"
        if any(marker in hierarchy for marker in self.profile.login_markers):
            return "login_required"
        return None

    def _is_shop(self, device: Any, hierarchy: str) -> bool:
        return self._is_shop_activity(device) or self._is_shop_hierarchy(hierarchy)

    def _is_shop_activity(self, device: Any) -> bool:
        activity = str(device.app_current().get("activity") or "")
        return any(marker in activity for marker in self.profile.shop_activity_markers)

    def _is_profile_activity(self, device: Any) -> bool:
        activity = str(device.app_current().get("activity") or "")
        return any(
            marker in activity for marker in self.profile.profile_activity_markers
        )

    def _is_detail(self, device: Any, hierarchy: str) -> bool:
        activity = str(device.app_current().get("activity") or "")
        return any(
            marker in activity for marker in self.profile.detail_activity_markers
        ) or self._is_detail_hierarchy(hierarchy)

    def _is_shop_hierarchy(self, hierarchy: str) -> bool:
        return any(marker in hierarchy for marker in self.profile.shop_markers)

    def _is_detail_hierarchy(self, hierarchy: str) -> bool:
        return any(marker in hierarchy for marker in self.profile.detail_markers)

    def _is_share_hierarchy(self, hierarchy: str) -> bool:
        return any(
            selector.value in hierarchy for selector in self.profile.copy_link
        )

    def _is_end(self, hierarchy: str) -> bool:
        return any(marker in hierarchy for marker in self.profile.end_markers)

    def _click_selector(
        self,
        device: Any,
        selectors: tuple[SelectorQuery, ...],
        job_id: str | None,
    ) -> Literal["clicked", "not_found", "cancelled"]:
        return _click_first(
            device,
            selectors,
            timeout_seconds=self.selector_timeout_seconds,
            poll_interval=self.transition_poll_interval,
            sleep=self._sleep,
            monotonic=self._monotonic,
            is_cancelled=lambda: self._is_cancelled(job_id),
        )

    def _fresh_clipboard_product_url(
        self,
        device: Any,
        *,
        previous_clipboard: Any,
        job_id: str | None,
    ) -> tuple[str | None, str]:
        previous_url = _canonical_product_url(previous_clipboard)
        deadline = self._monotonic() + self.clipboard_timeout_seconds
        first_attempt = True
        saw_stale_valid_url = False
        while first_attempt or self._monotonic() < deadline:
            first_attempt = False
            if self._is_cancelled(job_id):
                return None, "cancelled"
            current_url = _canonical_product_url(_read_clipboard(device))
            if self._is_cancelled(job_id):
                return None, "cancelled"
            if current_url is not None and current_url != previous_url:
                return current_url, ""
            if current_url is not None and current_url == previous_url:
                saw_stale_valid_url = True
            if self._monotonic() >= deadline:
                break
            self._sleep(
                min(
                    self.transition_poll_interval,
                    max(deadline - self._monotonic(), 0),
                )
            )
        return (
            None,
            "stale_clipboard" if saw_stale_valid_url else "product_link_unavailable",
        )

    def _wait_for_duration_or_cancellation(
        self, job_id: str | None, duration: float
    ) -> bool:
        deadline = self._monotonic() + max(duration, 0)
        while self._monotonic() < deadline:
            if self._is_cancelled(job_id):
                return True
            remaining = max(deadline - self._monotonic(), 0)
            interval = self.transition_poll_interval or remaining
            self._sleep(min(interval, remaining))
        return self._is_cancelled(job_id)

    @staticmethod
    def _open_profile(device: Any, account_user_id: str) -> None:
        url = f"https://www.xiaohongshu.com/user/profile/{account_user_id}"
        shell = getattr(device, "shell", None)
        if callable(shell):
            shell(
                [
                    "am",
                    "start",
                    "-a",
                    "android.intent.action.VIEW",
                    "-d",
                    url,
                    "-p",
                    "com.xingin.xhs",
                ]
            )
            return
        open_url = getattr(device, "open_url", None)
        if callable(open_url):
            open_url(url)
            return
        raise RuntimeError("uiautomator2 device cannot open a profile URL.")

    def _is_cancelled(self, job_id: str | None) -> bool:
        callback = getattr(self._cancellation_context, "callback", None)
        if callable(callback):
            try:
                if bool(callback()):
                    return True
            except Exception:
                return True
        return bool(
            job_id is not None
            and self.job_service is not None
            and self.job_service.get(job_id).state is JobState.cancelled
        )

    def _cancelled_result(
        self,
        request: CollectionRequest,
        job_id: str | None,
        items: list[CollectionItem],
        rejected_items: list[RejectedCollectionItem],
        artifacts: list[str],
        transitions: list[dict[str, Any]],
    ) -> CollectionResult | None:
        if not self._is_cancelled(job_id):
            return None
        return self._result(
            request=request,
            status="failed",
            detail="cancelled",
            items=items,
            rejected_items=rejected_items,
            artifacts=artifacts,
            transitions=transitions,
        )

    def _result(
        self,
        *,
        request: CollectionRequest,
        status: Literal["succeeded", "partial", "needs_human", "failed"],
        detail: str | None,
        items: list[CollectionItem],
        rejected_items: list[RejectedCollectionItem],
        artifacts: list[str],
        transitions: list[dict[str, Any]],
    ) -> CollectionResult:
        items = list(items)
        rejected_items = list(rejected_items)
        expected = request.expected_count
        observed = len(items) + len(rejected_items)
        duplicate_observations = sum(
            item.reason == "duplicate_source_url" for item in rejected_items
        )
        identity_observed = observed - duplicate_observations
        non_duplicate_rejections = len(rejected_items) - duplicate_observations
        if (
            status != "succeeded"
            and expected is not None
            and expected > 0
            and identity_observed == expected
            and non_duplicate_rejections == 0
            and items
        ):
            terminal_item = items.pop()
            rejected_items.append(
                RejectedCollectionItem(
                    reference=f"shop_product:{terminal_item.id}:terminal",
                    reason=detail or status,
                    raw_evidence={
                        "normalized_item": terminal_item.model_dump(mode="json"),
                        "transitions": transitions,
                    },
                )
            )
            observed = len(items) + len(rejected_items)
            non_duplicate_rejections += 1
        identity_observed = observed - duplicate_observations
        missing_count = (
            max((expected or 0) - identity_observed, 0)
            if expected is not None
            else 0
        )
        missing_reason = (
            "expected_product_not_discovered"
            if detail in {"expected_products_missing", None}
            else detail
        ) or "expected_product_not_discovered"
        missing_items = [
            MissingCollectionItem(
                reference=f"expected_product:{identity_observed + index + 1}",
                reason=missing_reason,
                raw_evidence={
                    "selector_profile_version": self.selector_profile_version,
                    "transitions": transitions,
                },
            )
            for index in range(missing_count)
        ]
        overflow_count = (
            max(identity_observed - expected, 0) if expected is not None else 0
        )
        complete = (
            status == "succeeded"
            and expected is not None
            and identity_observed == expected
            and non_duplicate_rejections == 0
        )
        return CollectionResult(
            status=status,
            detail=detail,
            evidence_artifacts=list(artifacts),
            items=items,
            rejected_items=rejected_items,
            expected_count_known=expected is not None,
            expected_count=expected,
            succeeded_count=len(items),
            observed_count=observed,
            raw_observation_count=observed,
            duplicate_observation_count=duplicate_observations,
            missing_items=missing_items,
            overflow_count=overflow_count,
            complete=complete,
        )


def _default_adb_client() -> Any:
    from adbutils import adb

    return adb


def _default_u2_connect(serial: str) -> Any:
    import uiautomator2 as u2

    return u2.connect(serial)


def _dependency_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for distribution in ("adbutils", "uiautomator2"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


def _select_device(devices: list[Any], selected_id: str | None) -> Any | None:
    if selected_id is not None:
        return next(
            (
                device
                for device in devices
                if str(getattr(device, "serial", "")) == selected_id
            ),
            None,
        )
    return devices[0] if len(devices) == 1 else None


def _device_reservation(serial: str) -> Lock:
    with _DEVICE_RESERVATIONS_GUARD:
        return _DEVICE_RESERVATIONS.setdefault(serial, Lock())


def _device_record(device: Any) -> dict[str, str]:
    serial = str(getattr(device, "serial", ""))
    try:
        state = str(device.get_state())
    except Exception as error:
        state = f"error:{type(error).__name__}"
    return {"device_id": serial, "state": state}


def _click_first(
    device: Any,
    selectors: tuple[SelectorQuery, ...],
    *,
    timeout_seconds: float,
    poll_interval: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    is_cancelled: Callable[[], bool],
) -> Literal["clicked", "not_found", "cancelled"]:
    deadline = monotonic() + max(timeout_seconds, 0)
    first_attempt = True
    while first_attempt or monotonic() < deadline:
        first_attempt = False
        if is_cancelled():
            return "cancelled"
        for selector in selectors:
            ui_object = device(**{selector.attribute: selector.value})
            exists_value = getattr(ui_object, "exists", False)
            remaining = max(deadline - monotonic(), 0)
            exists = (
                exists_value(timeout=min(max(poll_interval, 0), remaining))
                if callable(exists_value)
                else bool(exists_value)
            )
            if is_cancelled():
                return "cancelled"
            if exists:
                if is_cancelled():
                    return "cancelled"
                ui_object.click()
                return "clicked"
        if monotonic() >= deadline:
            break
        sleep(min(max(poll_interval, 0), max(deadline - monotonic(), 0)))
    return "not_found"


def _read_clipboard(device: Any) -> Any:
    try:
        return _read_clipboard_once(device)
    except Exception as original_error:
        current_ime = getattr(device, "current_ime", None)
        set_input_ime = getattr(device, "set_input_ime", None)
        shell = getattr(device, "shell", None)
        if not all(callable(method) for method in (current_ime, set_input_ime, shell)):
            raise
        previous_ime = current_ime()
        if not isinstance(previous_ime, str) or not previous_ime.strip():
            raise original_error
        try:
            set_input_ime()
            return _read_clipboard_via_input_ime(device)
        finally:
            shell(["ime", "set", previous_ime])
            shell(
                [
                    "settings",
                    "put",
                    "secure",
                    "default_input_method",
                    previous_ime,
                ]
            )


def _read_clipboard_once(device: Any) -> Any:
    value = getattr(device, "clipboard", None)
    if callable(value):
        value = value()
    if value is not None:
        return value
    getter = getattr(device, "get_clipboard", None)
    return getter() if callable(getter) else None


def _read_clipboard_via_input_ime(device: Any) -> str:
    last_error: ValueError | None = None
    for _ in range(3):
        response = device.shell(
            ["am", "broadcast", "-a", "ADB_KEYBOARD_GET_CLIPBOARD"]
        )
        output = str(getattr(response, "output", ""))
        result_match = re.search(r"result=(-?\d+)", output)
        data_match = re.search(r'data="([^"]*)"', output)
        if (
            result_match is None
            or int(result_match.group(1)) != -1
            or data_match is None
        ):
            last_error = ValueError("Android InputIME clipboard broadcast failed.")
            continue
        try:
            return base64.b64decode(data_match.group(1), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            last_error = ValueError("Android InputIME clipboard payload was invalid.")
    raise last_error or ValueError("Android InputIME clipboard broadcast failed.")


def _canonical_product_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or "\\" in candidate:
        return None
    if any(char.isspace() for char in candidate):
        embedded_urls = re.findall(r"https://[^\s]+", candidate)
        if len(embedded_urls) != 1:
            return None
        candidate = embedded_urls[0]
    try:
        parts = urlsplit(candidate)
        hostname = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except (TypeError, ValueError, UnicodeError):
        return None
    if (
        parts.scheme.lower() != "https"
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.fragment
    ):
        return None
    if hostname != "xhslink.com" and hostname != "xiaohongshu.com" and not hostname.endswith(
        ".xiaohongshu.com"
    ):
        return None
    if not parts.path or parts.path == "/":
        return None
    return urlunsplit(("https", hostname, parts.path, parts.query, ""))


def _rejected_product(
    product: ShopProductPosition,
    reason: str,
    raw_evidence: dict[str, Any],
    *,
    reference: str,
) -> RejectedCollectionItem:
    return RejectedCollectionItem(
        reference=reference,
        reason=reason,
        raw_evidence={
            "title": product.title,
            "price": product.price,
            "sold": product.sold,
            **raw_evidence,
        },
    )


def _screenshot_bytes(device: Any) -> bytes:
    value = device.screenshot()
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    save = getattr(value, "save", None)
    if callable(save):
        stream = BytesIO()
        save(stream, format="PNG")
        return stream.getvalue()
    raise ValueError("Android screenshot was not bytes or an image object.")


def _clean_text(value: str) -> str:
    return value.translate({ord(character): None for character in _ZERO_WIDTH}).strip()


def _bounds(value: str) -> tuple[int, int, int, int] | None:
    numbers = [int(number) for number in re.findall(r"\d+", value)]
    return tuple(numbers) if len(numbers) == 4 else None  # type: ignore[return-value]


def _safe_token(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    return normalized if _SAFE_TOKEN.fullmatch(normalized) is not None else None


def _is_disconnect_error(error: Exception) -> bool:
    if isinstance(error, (_DeviceDisconnected, ConnectionError, BrokenPipeError)):
        return True
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "device offline",
            "device disconnected",
            "device not found",
            "no devices",
            "closed connection",
        )
    )


def _error_evidence(error: Exception) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}
