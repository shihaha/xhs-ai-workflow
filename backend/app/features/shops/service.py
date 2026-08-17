"""Shop collection orchestration and tutorial-compatible N/N verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from backend.app.adapters.android_device import DEFAULT_SELECTOR_PROFILE_VERSION
from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
)
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


_IMAGE_EXTENSIONS = {".webp", ".png", ".jpg", ".jpeg"}


class ShopVerificationMissing(BaseModel):
    reference: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=500)
    product_dir: str | None = Field(default=None, max_length=500)


class ShopVerificationResult(BaseModel):
    expected_count: int = Field(ge=0)
    discovered_count: int = Field(ge=0)
    succeeded_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    missing_items: list[ShopVerificationMissing] = Field(default_factory=list)
    overflow_count: int = Field(default=0, ge=0)
    issues: list[str] = Field(default_factory=list)
    complete: bool = False


class ShopCollectionCreate(BaseModel):
    account_user_id: str = Field(
        min_length=1, max_length=500, pattern=r"^[A-Za-z0-9_-]+$"
    )
    account_name: str = Field(min_length=1, max_length=500)
    expected_count: int = Field(ge=0)
    device_id: str | None = Field(default=None, min_length=1, max_length=500)
    selector_profile_version: str = Field(
        default=DEFAULT_SELECTOR_PROFILE_VERSION, min_length=1, max_length=100
    )


class ShopCollectionMissing(BaseModel):
    reference: str
    reason: str
    raw_evidence: dict[str, Any]


class ShopCollectionRead(BaseModel):
    job_id: str
    status: Literal["succeeded", "partial", "needs_human", "failed"]
    detail: str | None
    selector_profile_version: str
    expected_count: int
    discovered_count: int
    succeeded_count: int
    missing_count: int
    missing_items: list[ShopCollectionMissing]
    overflow_count: int
    evidence_artifacts: list[str]
    items: list[CollectionItem]
    complete: bool


class ShopCollectionService:
    """Create a durable job, run one bounded device pass and finalize factual state."""

    def __init__(self, *, job_service: JobService, device_adapter: Any) -> None:
        self.job_service = job_service
        self.device_adapter = device_adapter

    def collect(self, payload: ShopCollectionCreate) -> ShopCollectionRead:
        input_data = payload.model_dump(mode="json")
        job = self.job_service.create(
            job_type="shop_collection",
            input_data=input_data,
            progress_total=payload.expected_count,
            current_stage="device_pending",
        )
        self.job_service.claim(job.id)
        try:
            result = self.device_adapter.collect_shop(
                CollectionRequest(
                    capability="shop_products",
                    parameters={
                        "account_user_id": payload.account_user_id,
                        "account_name": payload.account_name,
                        "device_id": payload.device_id,
                        "selector_profile_version": payload.selector_profile_version,
                        "job_id": job.id,
                    },
                    expected_count=payload.expected_count,
                )
            )
        except Exception as error:
            self.job_service.append_log(
                job.id,
                level="error",
                message=f"Shop collection raised: {type(error).__name__}: {error}",
            )
            if self.job_service.get(job.id).state is JobState.running:
                self.job_service.transition(
                    job.id,
                    JobState.failed,
                    current_stage="shop_failed",
                    error_category="shop_collection_failed",
                )
            raise

        self._finalize(job.id, result)
        return _shop_collection_read(
            job_id=job.id,
            selector_profile_version=payload.selector_profile_version,
            expected_count=payload.expected_count,
            result=result,
        )

    def _finalize(self, job_id: str, result: CollectionResult) -> None:
        if self.job_service.get(job_id).state is JobState.cancelled:
            return
        if result.status == "succeeded":
            self.job_service.transition(
                job_id,
                JobState.succeeded,
                progress_current=result.succeeded_count,
                progress_total=result.expected_count,
                current_stage="shop_complete",
            )
        elif result.status == "failed":
            self.job_service.transition(
                job_id,
                JobState.failed,
                progress_current=result.succeeded_count,
                progress_total=result.expected_count,
                current_stage="shop_failed",
                error_category=result.detail or "shop_collection_failed",
            )
        else:
            self.job_service.transition(
                job_id,
                JobState.needs_human,
                progress_current=result.succeeded_count,
                progress_total=result.expected_count,
                current_stage="shop_needs_human",
                error_category=result.detail or result.status,
            )


def verify_shop_collection(
    account_dir: Path | str, *, expected_count: int
) -> ShopVerificationResult:
    """Verify exact product evidence without converting partial artifacts into N/N."""
    if isinstance(expected_count, bool) or expected_count < 0:
        raise ValueError("expected_count must be a non-negative integer.")
    account_path = Path(account_dir).resolve()
    collection = _load_json(account_path / "collection.json", "collection.json")
    entries = collection.get("products")
    if not isinstance(entries, list):
        raise ValueError("collection.products must be a list.")

    issues: list[str] = []
    declared_count = collection.get("unique_product_link_count")
    if (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count != len(entries)
    ):
        issues.append("declared_discovered_count_mismatch")
    if declared_count != expected_count:
        issues.append("declared_expected_count_mismatch")

    missing: list[ShopVerificationMissing] = []
    seen_urls: set[str] = set()
    seen_dirs: set[str] = set()
    succeeded = 0
    for index, entry in enumerate(entries):
        reference = f"collection_product:{index + 1}"
        if not isinstance(entry, dict):
            missing.append(
                ShopVerificationMissing(
                    reference=reference, reason="invalid_product_entry"
                )
            )
            continue
        source_url = _text(entry.get("source_url"))
        product_dir = _text(entry.get("product_dir"))
        if source_url is None or not _is_https_url(source_url):
            missing.append(
                ShopVerificationMissing(
                    reference=reference,
                    reason="invalid_source_url",
                    product_dir=product_dir,
                )
            )
            continue
        if source_url in seen_urls:
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason="duplicate_source_url",
                    product_dir=product_dir,
                )
            )
            continue
        seen_urls.add(source_url)
        if product_dir is None or not _safe_product_dir(product_dir):
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason="invalid_product_dir",
                    product_dir=product_dir,
                )
            )
            continue
        if product_dir in seen_dirs:
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason="duplicate_product_dir",
                    product_dir=product_dir,
                )
            )
            continue
        seen_dirs.add(product_dir)
        product_path = (account_path / product_dir).resolve()
        try:
            product_path.relative_to(account_path)
        except ValueError:
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason="invalid_product_dir",
                    product_dir=product_dir,
                )
            )
            continue
        reason = _verify_product(product_path, source_url)
        if reason is not None:
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason=reason,
                    product_dir=product_dir,
                )
            )
            continue
        succeeded += 1

    discovered = len(entries)
    for index in range(discovered, expected_count):
        missing.append(
            ShopVerificationMissing(
                reference=f"expected_product:{index + 1}",
                reason="expected_product_not_discovered",
            )
        )
    overflow = max(discovered - expected_count, 0)
    complete = (
        not issues
        and overflow == 0
        and discovered == expected_count
        and succeeded == expected_count
        and not missing
    )
    return ShopVerificationResult(
        expected_count=expected_count,
        discovered_count=discovered,
        succeeded_count=succeeded,
        missing_count=len(missing),
        missing_items=missing,
        overflow_count=overflow,
        issues=issues,
        complete=complete,
    )


def _shop_collection_read(
    *,
    job_id: str,
    selector_profile_version: str,
    expected_count: int,
    result: CollectionResult,
) -> ShopCollectionRead:
    desired_missing_count = max(expected_count - result.succeeded_count, 0)
    candidates = [
        ShopCollectionMissing(
            reference=item.reference,
            reason=item.reason,
            raw_evidence=item.raw_evidence,
        )
        for item in result.rejected_items
    ] + [
        ShopCollectionMissing(
            reference=item.reference,
            reason=item.reason,
            raw_evidence=item.raw_evidence,
        )
        for item in result.missing_items
    ]
    while len(candidates) < desired_missing_count:
        candidates.append(
            ShopCollectionMissing(
                reference=f"expected_product:{len(candidates) + 1}",
                reason="expected_product_not_collected",
                raw_evidence={"job_id": job_id},
            )
        )
    return ShopCollectionRead(
        job_id=job_id,
        status=result.status,
        detail=result.detail,
        selector_profile_version=selector_profile_version,
        expected_count=expected_count,
        discovered_count=int(result.observed_count or 0),
        succeeded_count=result.succeeded_count,
        missing_count=desired_missing_count,
        missing_items=candidates[:desired_missing_count],
        overflow_count=result.overflow_count,
        evidence_artifacts=result.evidence_artifacts,
        items=result.items,
        complete=result.complete,
    )


def _verify_product(product_dir: Path, source_url: str) -> str | None:
    detail_path = product_dir / "detail.json"
    if not detail_path.is_file():
        return "detail_json_missing"
    try:
        detail = _load_json(detail_path, "detail.json")
    except ValueError:
        return "detail_json_invalid"
    if _text(detail.get("link")) != source_url:
        return "detail_url_mismatch"
    images_dir = product_dir / "images"
    if not images_dir.is_dir():
        return "images_directory_missing"
    images = [
        path
        for path in sorted(images_dir.iterdir())
        if path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS
    ]
    if not images:
        return "local_images_missing"
    manifest = detail.get("image_manifest")
    if not isinstance(manifest, list):
        return "image_manifest_missing"
    records: dict[str, str] = {}
    for item in manifest:
        if not isinstance(item, dict):
            return "image_manifest_invalid"
        relative = _text(item.get("file"))
        digest = _text(item.get("sha256"))
        if relative is None or digest is None or relative in records:
            return "image_manifest_invalid"
        records[relative] = digest
    expected_files = {f"images/{image.name}" for image in images}
    if set(records) != expected_files:
        return "image_manifest_incomplete"
    for image in images:
        relative = f"images/{image.name}"
        if hashlib.sha256(image.read_bytes()).hexdigest() != records[relative]:
            return "image_hash_mismatch"
    return None


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} cannot be read: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _safe_product_dir(value: str) -> bool:
    candidate = Path(value)
    return (
        value not in {".", ".."}
        and candidate.name == value
        and "/" not in value
        and "\\" not in value
    )


def _is_https_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return (
            parts.scheme.lower() == "https"
            and parts.hostname is not None
            and parts.username is None
            and parts.password is None
            and parts.port is None
            and not parts.fragment
        )
    except (TypeError, ValueError, UnicodeError):
        return False


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
