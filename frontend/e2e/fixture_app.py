"""Controlled Task 9 browser fixture: temporary SQLite, deterministic adapters, no live calls."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionResult,
    DeviceHealth,
    GeneratedImage,
    ModelResult,
    VisionResult,
    VisualAssessment,
)
from backend.app.adapters.qianfan_playwright import QIANFAN_RANK_URL, QianfanSelectorProfile
from backend.app.adapters.xhs_cli_read import XhsCliReadAdapter
from backend.app.features.analysis.service import AnalysisService
from backend.app.features.content.service import ContentService
from backend.app.features.media.service import ContentMediaService
from backend.app.features.media.worker import ContentMediaWorker
from backend.app.features.radar.qianfan_service import QianfanCollectionService
from backend.app.features.radar.service import RadarService
from backend.app.features.shops.service import ShopCollectionService
from backend.app.features.xhs.service import XhsCollectionService
from backend.app.main import create_app
from backend.app.settings import Settings


_TEMP = tempfile.TemporaryDirectory(prefix="xhs-task9-e2e-")
RUNTIME = Path(_TEMP.name).resolve()
FIXTURES = RUNTIME / "fixtures"
SHOP = FIXTURES / "shop-account"
PRODUCT = SHOP / "product-1"
IMAGE = PRODUCT / "images" / "product.png"
SOURCE_URL = "https://www.xiaohongshu.com/explore/product-1"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

IMAGE.parent.mkdir(parents=True, exist_ok=True)
IMAGE.write_bytes(_PNG)
(FIXTURES / "source.txt").write_text("controlled Task 9 source evidence\n", encoding="utf-8")
(SHOP / "collection.json").write_text(json.dumps({
    "unique_product_link_count": 1,
    "products": [{"source_url": SOURCE_URL, "product_dir": "product-1"}],
}), encoding="utf-8")
(PRODUCT / "detail.json").write_text(json.dumps({
    "link": SOURCE_URL,
    "image_manifest": [{"file": "images/product.png", "sha256": hashlib.sha256(_PNG).hexdigest()}],
}), encoding="utf-8")


class ControlledModel:
    configured = True
    provider = "controlled-task9"
    model = "controlled-task9-model"

    def generate_structured(self, request, schema):
        if schema.__name__ == "AnalysisOutput":
            context = json.loads(request.user_prompt)
            account_ids = context.get("account_user_ids") or [context.get("account_user_id")]
            account_suffix = f"{account_ids[0]}-{request.evidence_ids[0]}"
            shop_evidence_ids = [item for item in request.evidence_ids if item.startswith("artifact:")]
            output = {
                "claims": [{"claim": "受控证据证明需求存在", "evidence_ids": request.evidence_ids}],
                "product_clusters": [{"name": "露营收纳", "summary": "受控聚类", "evidence_ids": request.evidence_ids}],
                "opportunities": [{"title": f"受控露营收纳机会-{account_suffix}", "status": "升温", "summary": "深度核验商品证据支持", "evidence_ids": shop_evidence_ids, "next_action": "创建受控产品任务"}],
            }
        else:
            context = json.loads(request.user_prompt)
            image_ids = context["ordered_image_material_ids"]
            output = {
                "title": f"{context['product']['name']}清单",
                "body": "这是只使用受控持久证据生成的待审核正文。",
                "claims": [{"claim": "需求存在", "evidence_ids": request.evidence_ids}],
                "source_evidence_ids": request.evidence_ids,
                "image_plan": [{"page_number": index + 1, "material_id": material_id, "role": "cover" if index == 0 else "page", "headline": "受控露营清单", "visual_direction": "清晰、可人工核验"} for index, material_id in enumerate(image_ids)],
            }
        return ModelResult(model=self.model, output=output, raw_evidence={"attempts": []}, usage={"input_tokens": 1, "output_tokens": 1}, duration_ms=1)


class ControlledDevice:
    def health(self):
        return DeviceHealth(status="available", device_id="controlled-serial", detail="controlled_device_ready", raw_evidence={"fixture": "task9"})

    def collect_shop(self, request):
        expected = request.expected_count
        assert expected == 1
        item = CollectionItem(id="product-1", kind="shop_product", source_url=SOURCE_URL, raw_evidence={"fixture": "task9-device"}, data={"title": "受控商品"})
        return CollectionResult(status="succeeded", evidence_artifacts=["fixtures/shop-account"], items=[item], expected_count_known=True, expected_count=1, succeeded_count=1, raw_observation_count=1, missing_items=[], overflow_count=0, complete=True)


class ControlledImageGeneration:
    configured = True
    provider = "controlled-media"
    model = "controlled-image-v1"

    def generate_images(self, request):
        assert request.prompt
        return [GeneratedImage(
            data=_PNG,
            mime_type="image/png",
            width=1,
            height=1,
            sha256=hashlib.sha256(_PNG).hexdigest(),
            provider_request_id="controlled-image-request",
            usage={"images": 1},
            duration_ms=1,
            raw_evidence={"attempts": [{"attempt": 1, "category": "succeeded"}]},
        )]


class ControlledVision:
    configured = True
    provider = "controlled-media"
    model = "controlled-vision-v1"

    def analyze_images(self, request, schema):
        assert schema is VisualAssessment
        assert request.images and request.material_ids == [image.material_id for image in request.images]
        return VisionResult(
            model=self.model,
            output=VisualAssessment(
                summary="受控视觉检查建议人工确认",
                plan_match=True,
                text_readability="受控图片可解码；文字仍需人工核验",
                defects=[],
                safety_issues=[],
                suggestions=["人工确认构图、裁切和文字"],
            ),
            raw_evidence={
                "provider_request_id": "controlled-vision-request",
                "attempts": [{"attempt": 1, "category": "succeeded"}],
            },
            usage={"input_images": len(request.images)},
            duration_ms=1,
        )


def controlled_xhs_cli(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    """Return the pinned CLI's real JSON shapes without any live process or network."""
    assert kwargs["shell"] is False
    assert Path(str(kwargs["cwd"])).resolve() == settings.xhs_cli_state_dir / "private-runtime"
    child_env = kwargs["env"]
    assert isinstance(child_env, dict) and "PARENT_SECRET_SENTINEL" not in child_env
    wrapper = Path(__import__("backend.app.adapters.xhs_cli_readonly_wrapper", fromlist=["__file__"]).__file__).resolve()
    assert argv[:3] == [sys.executable, "-I", str(wrapper)]
    assert "controlled-a1" in kwargs["input_bytes"].decode("utf-8")
    assert "controlled-a1" not in " ".join(argv)
    command = argv[3:]
    if command[:1] == ["user"] and command[-1:] == ["--json"]:
        user_id = command[1]
        payload: object = {
            "userPageData": {
                "basicInfo": {
                    "userId": user_id,
                    "nickname": f"受控账号资料-{user_id}",
                    "desc": "受控公开简介",
                },
                "interactions": [{"name": "fans", "count": 12}],
            },
            "userInfo": {"userId": user_id, "guest": False},
        }
    elif command[:1] == ["user-posts"] and command[-1:] == ["--json"]:
        user_id = command[1]
        payload = [{
            "id": f"note-{user_id}",
            "xsecToken": "controlled-xsec-must-not-persist",
            "noteCard": {
                "displayTitle": "受控露营收纳笔记",
                "desc": "受控公开笔记摘要",
                "publishTime": "2026-08-18T10:00:00Z",
                "user": {"userId": user_id, "nickname": f"受控账号资料-{user_id}"},
                "interactInfo": {"likedCount": 7},
            },
        }]
    elif command[:1] == ["search"] and command[-1:] == ["--json"]:
        payload = []
    else:
        raise AssertionError(f"unexpected controlled xhs command: {command!r}")
    return subprocess.CompletedProcess(
        argv,
        0,
        stdout=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stderr=b"",
    )


CONTROLLED_QIANFAN_PROFILE = QianfanSelectorProfile(
    version="controlled-qianfan-v1", supported=True,
    ready_selector="#ready", login_selector="#login", captcha_selector="#captcha",
    board_selectors=(("阅读榜", "#read"), ("引流榜", "#traffic"), ("热卖榜", "#sales"), ("成交榜", "#orders")),
    dimension_selectors=(("优秀内容", "#content"), ("优秀账号", "#accounts")),
    active_board_selectors=(("阅读榜", "#read.active"), ("引流榜", "#traffic.active"), ("热卖榜", "#sales.active"), ("成交榜", "#orders.active")),
    active_dimension_selectors=(("优秀内容", "#content.active"), ("优秀账号", "#accounts.active")),
    response_board_path=("scope", "board"), response_dimension_path=("scope", "dimension"),
)


class ControlledQianfan:
    def __init__(self, job_service):
        self.job_service = job_service

    def collect_scope(self, request, *, board, dimension, selector_profile):
        job_id = request.parameters["job_id"]
        collection_id = str(self.job_service.get(job_id).input["collection_id"])
        account_id = f"controlled-account-{collection_id}"
        payload = json.dumps({"scope": {"board": board, "dimension": dimension}, "fixture": "task9"}, ensure_ascii=False).encode("utf-8")
        relative = Path("evidence") / "qianfan" / f"{job_id}.json"
        absolute = RUNTIME / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(payload)
        self.job_service.attach_artifact(job_id, kind="qianfan_raw_capture", path=relative.as_posix(), metadata={
            "sha256": hashlib.sha256(payload).hexdigest(), "source_url": QIANFAN_RANK_URL,
            "selector_profile_version": selector_profile.version, "board": board, "dimension": dimension,
        })
        item = CollectionItem(id=f"{board}-{dimension}", kind="ranking_item", source_url=f"https://www.xiaohongshu.com/explore/{job_id}", raw_evidence={"fixture": "task9-qianfan"}, data={
            "rank": 1, "author_name": f"受控千帆账号-{collection_id}", "user_id": account_id, "note_id": job_id,
            "read_range": "10万以上", "pay_rate_range": "70%-90%", "gmv_range": "1万-5万",
        })
        return CollectionResult(status="succeeded", evidence_artifacts=[relative.as_posix()], items=[item], expected_count_known=True, expected_count=1, succeeded_count=1, raw_observation_count=1, missing_items=[], overflow_count=0, complete=True)


settings = Settings(runtime_dir=RUNTIME, database_path=RUNTIME / "task9.sqlite3", bailian_api_key="controlled-not-live")
assert settings.xhs_cli_state_dir is not None
xhs_config = settings.xhs_cli_state_dir / ".xhs-cli"
xhs_config.mkdir(parents=True, exist_ok=True)
(xhs_config / "cookies.json").write_text(
    json.dumps({"cookies": {"a1": "controlled-a1", "web_session": "controlled-session"}}),
    encoding="utf-8",
)
app = create_app(settings)
model = ControlledModel()
app.state.bailian_adapter = model
app.state.analysis_service = AnalysisService(app.state.database, model, runtime_dir=RUNTIME)
app.state.content_service = ContentService(app.state.database, model, runtime_dir=RUNTIME, cleanup_service=app.state.artifact_cleanup_service)
app.state.bailian_image_adapter = ControlledImageGeneration()
app.state.bailian_vision_adapter = ControlledVision()
app.state.content_media_service = ContentMediaService(
    app.state.database,
    content_service=app.state.content_service,
    image_adapter=app.state.bailian_image_adapter,
    vision_adapter=app.state.bailian_vision_adapter,
    runtime_dir=RUNTIME,
    cleanup_service=app.state.artifact_cleanup_service,
)
app.state.content_media_worker = ContentMediaWorker(
    app.state.content_media_service, poll_seconds=0.01
)
if app.state.xhs_collection_service is not None:
    app.state.xhs_collection_service.close()
app.state.xhs_collection_service = XhsCollectionService(
    database=app.state.database,
    job_service=app.state.job_service,
    adapter=XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=settings.xhs_cli_state_dir,
        runtime_dir=RUNTIME,
        runner=controlled_xhs_cli,
    ),
    runtime_dir=RUNTIME,
)
if app.state.qianfan_collection_service is not None:
    app.state.qianfan_collection_service.close()
app.state.qianfan_collection_service = QianfanCollectionService(
    job_service=app.state.job_service, radar_service=RadarService(app.state.database), runtime_dir=RUNTIME,
    adapter_factory=lambda: ControlledQianfan(app.state.job_service), selector_profile=CONTROLLED_QIANFAN_PROFILE,
)
app.state.shop_service.close()
app.state.android_adapter = ControlledDevice()
app.state.shop_service = ShopCollectionService(job_service=app.state.job_service, device_adapter=app.state.android_adapter, max_workers=1)
