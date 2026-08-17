"""Shop collection orchestration and tutorial-compatible N/N verification."""

from __future__ import annotations

import hashlib
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, StrictInt, field_validator

from backend.app.adapters.android_device import DEFAULT_SELECTOR_PROFILE_VERSION
from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    RejectedCollectionItem,
)
from backend.app.models.jobs import JobState
from backend.app.services.jobs import InvalidJobTransition, JobService


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
    expected_count: StrictInt = Field(ge=0)
    device_id: str | None = Field(default=None, min_length=1, max_length=500)
    verification_dir: str | None = Field(default=None, min_length=1, max_length=1000)
    selector_profile_version: str = Field(
        default=DEFAULT_SELECTOR_PROFILE_VERSION, min_length=1, max_length=100
    )

    @field_validator("account_name", "device_id", "verification_dir")
    @classmethod
    def trim_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must contain non-whitespace text")
        return normalized


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
    rejected_count: int
    rejected_items: list[RejectedCollectionItem]
    overflow_count: int
    evidence_artifacts: list[str]
    items: list[CollectionItem]
    verification: ShopVerificationResult | None = None
    complete: bool


class ShopCollectionQueued(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"


class InvalidVerificationPath(ValueError):
    """Raised when supplied verification evidence is outside runtime storage."""


class ShopCollectionService:
    """Queue a durable job, run one bounded device pass and finalize factual state."""

    def __init__(
        self,
        *,
        job_service: JobService,
        device_adapter: Any,
        submitter: Callable[..., Any] | None = None,
        max_workers: int = 4,
    ) -> None:
        self.job_service = job_service
        self.device_adapter = device_adapter
        self._executor = (
            ThreadPoolExecutor(
                max_workers=max(1, max_workers),
                thread_name_prefix="shop-collection",
            )
            if submitter is None
            else None
        )
        self._submitter = submitter or self._executor.submit

    def enqueue(self, payload: ShopCollectionCreate) -> ShopCollectionQueued:
        self._resolve_verification_dir(payload.verification_dir)
        input_data = payload.model_dump(mode="json")
        job = self.job_service.create(
            job_type="shop_collection",
            input_data=input_data,
            progress_total=payload.expected_count,
            current_stage="device_pending",
        )
        try:
            self._submitter(self.execute, job.id, payload)
        except Exception as error:
            self.job_service.append_log(
                job.id,
                level="error",
                message=f"Shop collection scheduling raised: {type(error).__name__}: {error}",
            )
            self.job_service.claim(job.id)
            self.job_service.transition(
                job.id,
                JobState.failed,
                current_stage="shop_failed",
                error_category="shop_collection_schedule_failed",
            )
            raise
        return ShopCollectionQueued(job_id=job.id)

    def execute(
        self, job_id: str, payload: ShopCollectionCreate
    ) -> ShopCollectionRead | None:
        try:
            self.job_service.claim(job_id)
        except InvalidJobTransition:
            return None
        try:
            result = self.device_adapter.collect_shop(
                CollectionRequest(
                    capability="shop_products",
                    parameters={
                        "account_user_id": payload.account_user_id,
                        "account_name": payload.account_name,
                        "device_id": payload.device_id,
                        "selector_profile_version": payload.selector_profile_version,
                        "job_id": job_id,
                    },
                    expected_count=payload.expected_count,
                )
            )
        except Exception as error:
            self.job_service.append_log(
                job_id,
                level="error",
                message=f"Shop collection raised: {type(error).__name__}: {error}",
            )
            if self.job_service.get(job_id).state is JobState.running:
                self.job_service.transition(
                    job_id,
                    JobState.failed,
                    current_stage="shop_failed",
                    error_category="shop_collection_failed",
                )
            return None

        try:
            return self._complete_execution(job_id, payload, result)
        except Exception as error:
            self.job_service.append_log(
                job_id,
                level="error",
                message=(
                    "Shop collection background execution raised: "
                    f"{type(error).__name__}: {error}"
                ),
            )
            if self.job_service.get(job_id).state is JobState.running:
                self.job_service.transition(
                    job_id,
                    JobState.failed,
                    current_stage="shop_failed",
                    error_category="shop_collection_failed",
                )
            return None

    def _complete_execution(
        self,
        job_id: str,
        payload: ShopCollectionCreate,
        result: CollectionResult,
    ) -> ShopCollectionRead:
        read = _shop_collection_read(
            job_id=job_id,
            selector_profile_version=payload.selector_profile_version,
            expected_count=payload.expected_count,
            result=result,
        )
        if self.job_service.get(job_id).state is JobState.cancelled:
            read = _cancelled_shop_read(read)
        elif result.status == "succeeded" and payload.verification_dir is None:
            read = _verification_required(read)
        elif result.status == "succeeded":
            try:
                verification_dir = self._resolve_verification_dir(
                    payload.verification_dir
                )
                assert verification_dir is not None
                verification = verify_shop_collection(
                    verification_dir,
                    expected_count=payload.expected_count,
                    expected_source_urls=[str(item.source_url) for item in result.items],
                )
            except (InvalidVerificationPath, ValueError) as error:
                self.job_service.append_log(
                    job_id,
                    level="warning",
                    message=(
                        "Product evidence verification failed: "
                        f"{type(error).__name__}: {error}"
                    ),
                )
                read = read.model_copy(
                    update={
                        "status": "needs_human",
                        "detail": "product_evidence_verification_failed",
                        "complete": False,
                    }
                )
            else:
                read = read.model_copy(update={"verification": verification})
                if not verification.complete:
                    read = read.model_copy(
                        update={
                            "status": "needs_human",
                            "detail": "product_evidence_verification_failed",
                            "complete": False,
                        }
                    )
        if self.job_service.get(job_id).state is JobState.cancelled:
            read = _cancelled_shop_read(read)
        self._persist_result(job_id, read)
        self._finalize(job_id, read)
        return read

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=False)

    def _resolve_verification_dir(self, value: str | None) -> Path | None:
        if value is None:
            return None
        relative = Path(value)
        if relative.is_absolute():
            raise InvalidVerificationPath(
                "Verification directory must be relative to runtime storage."
            )
        try:
            resolved = (self.job_service.runtime_dir / relative).resolve(strict=True)
            resolved.relative_to(self.job_service.runtime_dir)
        except (OSError, ValueError) as error:
            raise InvalidVerificationPath(
                "Verification directory escapes or is missing from runtime storage."
            ) from error
        if not resolved.is_dir():
            raise InvalidVerificationPath("Verification directory is not a directory.")
        return resolved

    def _persist_result(self, job_id: str, result: ShopCollectionRead) -> None:
        relative = Path("evidence") / "shops" / job_id / "result.json"
        absolute = (self.job_service.runtime_dir / relative).resolve()
        try:
            absolute.relative_to(self.job_service.runtime_dir)
        except ValueError as error:
            raise RuntimeError("Shop result path escapes runtime storage.") from error
        absolute.parent.mkdir(parents=True, exist_ok=True)
        payload = result.model_dump(mode="json")
        absolute.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.job_service.attach_artifact(
            job_id,
            kind="shop_collection_result",
            path=relative.as_posix(),
            metadata={"result": payload},
        )

    def _finalize(self, job_id: str, result: ShopCollectionRead) -> None:
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
    account_dir: Path | str,
    *,
    expected_count: int,
    expected_source_urls: list[str] | None = None,
) -> ShopVerificationResult:
    """Verify exact product evidence without converting partial artifacts into N/N."""
    if isinstance(expected_count, bool) or expected_count < 0:
        raise ValueError("expected_count must be a non-negative integer.")
    try:
        account_path = Path(account_dir).resolve(strict=True)
    except OSError as error:
        raise ValueError(f"account directory cannot be resolved: {account_dir}") from error
    if not _is_regular_directory(account_path, roots=(account_path,)):
        raise ValueError(f"account directory is not a directory: {account_path}")
    collection_path = _resolved_regular_file(
        account_path / "collection.json", roots=(account_path,)
    )
    if collection_path is None:
        raise ValueError("collection.json is not a contained regular file.")
    collection = _load_json(collection_path, "collection.json")
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
    discovered_urls: list[str] = []
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
        discovered_urls.append(source_url)
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
        product_path = _resolved_directory(
            account_path / product_dir, roots=(account_path,)
        )
        if product_path is None:
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason="invalid_product_dir",
                    product_dir=product_dir,
                )
            )
            continue
        reason = _verify_product(product_path, source_url, account_path=account_path)
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
    if expected_source_urls is not None and (
        len(discovered_urls) != len(expected_source_urls)
        or set(discovered_urls) != set(expected_source_urls)
    ):
        issues.append("collected_source_url_mismatch")
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
    missing_items = [
        ShopCollectionMissing(
            reference=item.reference,
            reason=item.reason,
            raw_evidence=item.raw_evidence,
        )
        for item in result.missing_items
    ]
    return ShopCollectionRead(
        job_id=job_id,
        status=result.status,
        detail=result.detail,
        selector_profile_version=selector_profile_version,
        expected_count=expected_count,
        discovered_count=int(result.observed_count or 0),
        succeeded_count=result.succeeded_count,
        missing_count=len(missing_items),
        missing_items=missing_items,
        rejected_count=len(result.rejected_items),
        rejected_items=list(result.rejected_items),
        overflow_count=result.overflow_count,
        evidence_artifacts=result.evidence_artifacts,
        items=result.items,
        complete=result.complete,
    )


def _verification_required(result: ShopCollectionRead) -> ShopCollectionRead:
    return result.model_copy(
        update={
            "status": "needs_human",
            "detail": "product_evidence_verification_pending",
            "complete": False,
        }
    )


def _cancelled_shop_read(result: ShopCollectionRead) -> ShopCollectionRead:
    return result.model_copy(
        update={"status": "failed", "detail": "cancelled", "complete": False}
    )


def _verify_product(
    product_dir: Path, source_url: str, *, account_path: Path
) -> str | None:
    unresolved_detail = product_dir / "detail.json"
    if not unresolved_detail.exists() and not unresolved_detail.is_symlink():
        return "detail_json_missing"
    detail_path = _resolved_regular_file(
        unresolved_detail, roots=(account_path, product_dir)
    )
    if detail_path is None:
        return "detail_json_invalid_path"
    try:
        detail = _load_json(detail_path, "detail.json")
    except ValueError:
        return "detail_json_invalid"
    if _text(detail.get("link")) != source_url:
        return "detail_url_mismatch"
    unresolved_images_dir = product_dir / "images"
    if not unresolved_images_dir.exists() and not unresolved_images_dir.is_symlink():
        return "images_directory_missing"
    images_dir = _resolved_directory(
        unresolved_images_dir, roots=(account_path, product_dir)
    )
    if images_dir is None:
        return "images_directory_invalid_path"
    images: list[Path] = []
    for candidate in sorted(images_dir.iterdir()):
        if candidate.suffix.lower() not in _IMAGE_EXTENSIONS:
            continue
        image = _resolved_regular_file(
            candidate, roots=(account_path, product_dir, images_dir)
        )
        if image is None:
            return "image_file_invalid_path"
        images.append(image)
    if not images:
        return "local_images_missing"
    manifest = detail.get("image_manifest")
    if not isinstance(manifest, list):
        return "image_manifest_missing"
    records: dict[str, tuple[str, Path]] = {}
    for item in manifest:
        if not isinstance(item, dict):
            return "image_manifest_invalid"
        relative = _text(item.get("file"))
        digest = _text(item.get("sha256"))
        if relative is None or digest is None or relative in records:
            return "image_manifest_invalid"
        manifest_path = PurePosixPath(relative)
        if (
            "\\" in relative
            or manifest_path.is_absolute()
            or relative != manifest_path.as_posix()
            or len(manifest_path.parts) != 2
            or manifest_path.parts[0] != "images"
            or any(part in {".", ".."} for part in manifest_path.parts)
        ):
            return "image_manifest_invalid_path"
        resolved_manifest_path = _resolved_regular_file(
            product_dir.joinpath(*manifest_path.parts),
            roots=(account_path, product_dir, images_dir),
        )
        if resolved_manifest_path is None:
            return "image_manifest_invalid_path"
        records[relative] = (digest, resolved_manifest_path)
    expected_files = {f"images/{image.name}" for image in images}
    if set(records) != expected_files:
        return "image_manifest_incomplete"
    for image in images:
        relative = f"images/{image.name}"
        digest, manifest_image = records[relative]
        if manifest_image != image:
            return "image_manifest_invalid_path"
        if hashlib.sha256(image.read_bytes()).hexdigest() != digest:
            return "image_hash_mismatch"
    return None


def _resolved_regular_file(path: Path, *, roots: tuple[Path, ...]) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
        for root in roots:
            resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved if stat.S_ISREG(mode) else None


def _resolved_directory(path: Path, *, roots: tuple[Path, ...]) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    return resolved if _is_regular_directory(resolved, roots=roots) else None


def _is_regular_directory(path: Path, *, roots: tuple[Path, ...]) -> bool:
    try:
        mode = path.stat().st_mode
        for root in roots:
            path.relative_to(root)
    except (OSError, ValueError):
        return False
    return stat.S_ISDIR(mode)


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
