"""Controlled Task 9 browser fixture: temporary SQLite, deterministic adapters, no live calls."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from pathlib import Path

from backend.app.adapters.contracts import CollectionItem, CollectionResult, DeviceHealth, ModelResult
from backend.app.features.analysis.service import AnalysisService
from backend.app.features.content.service import ContentService
from backend.app.features.shops.service import ShopCollectionService
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
(FIXTURES / "cover.png").write_bytes(_PNG)
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
            account_suffix = account_ids[0]
            output = {
                "claims": [{"claim": "受控证据证明需求存在", "evidence_ids": request.evidence_ids}],
                "product_clusters": [{"name": "露营收纳", "summary": "受控聚类", "evidence_ids": request.evidence_ids}],
                "opportunities": [{"title": f"受控露营收纳机会-{account_suffix}", "status": "升温", "summary": "深度核验商品证据支持", "evidence_ids": request.evidence_ids, "next_action": "创建受控产品任务"}],
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


settings = Settings(runtime_dir=RUNTIME, database_path=RUNTIME / "task9.sqlite3", bailian_api_key="controlled-not-live")
app = create_app(settings)
model = ControlledModel()
app.state.bailian_adapter = model
app.state.analysis_service = AnalysisService(app.state.database, model, runtime_dir=RUNTIME)
app.state.content_service = ContentService(app.state.database, model, runtime_dir=RUNTIME, cleanup_service=app.state.artifact_cleanup_service)
app.state.shop_service.close()
app.state.android_adapter = ControlledDevice()
app.state.shop_service = ShopCollectionService(job_service=app.state.job_service, device_adapter=app.state.android_adapter, max_workers=1)
