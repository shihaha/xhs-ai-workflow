"""Durable per-product discovery facts for Android shop traversal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from backend.app.models.jobs import JobArtifactRecord
from backend.app.services.jobs import JobService


DISCOVERY_ARTIFACT_KIND = "android_shop_product_discovery"
DISCOVERY_ARTIFACT_PRODUCER = "android_shop_worker_v1"
_DISCOVERY_FILE = re.compile(r"^(\d{4})-([0-9a-f]{16})\.json$", re.ASCII)
_PAYLOAD_KEYS = {
    "schema_version",
    "discovery_job_id",
    "account_user_id",
    "product_stable_identity",
    "identity_basis",
    "source_url",
    "canonical_url",
    "title",
    "visible_metadata",
    "discovery_order",
    "discovered_at",
    "raw_evidence_references",
}


@dataclass(frozen=True)
class ShopProductDiscovery:
    artifact_id: int
    discovery_job_id: str
    account_user_id: str
    product_stable_identity: str
    identity_basis: str
    source_url: str
    canonical_url: str | None
    title: str
    visible_metadata: dict[str, str]
    discovery_order: int
    discovered_at: str
    raw_evidence_references: tuple[str, ...]
    artifact_path: str
    result_sha256: str


def persist_shop_product_discovery(
    job_service: JobService,
    *,
    job_id: str,
    account_user_id: str,
    source_url: str,
    title: str,
    visible_metadata: dict[str, str],
    discovery_order: int,
    raw_evidence_references: list[str],
) -> ShopProductDiscovery:
    """Write the discovery result before the caller proceeds to later work."""

    stable_identity = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    payload = {
        "schema_version": 1,
        "discovery_job_id": job_id,
        "account_user_id": account_user_id,
        "product_stable_identity": stable_identity,
        "identity_basis": "source_url_sha256_v1",
        "source_url": source_url,
        "canonical_url": None,
        "title": title,
        "visible_metadata": dict(visible_metadata),
        "discovery_order": discovery_order,
        "discovered_at": datetime.now(UTC).isoformat(),
        "raw_evidence_references": list(dict.fromkeys(raw_evidence_references)),
    }
    encoded = _canonical_bytes(payload)
    relative = (
        Path("evidence")
        / "android"
        / job_id
        / "discoveries"
        / f"{discovery_order:04d}-{stable_identity[:16]}.json"
    )
    absolute = _contained_discovery_path(job_service, relative)
    _write_once(absolute, encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    job_service.attach_artifact_once(
        job_id,
        kind=DISCOVERY_ARTIFACT_KIND,
        producer=DISCOVERY_ARTIFACT_PRODUCER,
        path=relative.as_posix(),
        metadata={"schema_version": 1, "sha256": digest, "result": payload},
    )
    return _read_bound_discoveries(
        job_service,
        job_id,
        expected_path=relative,
        expected_account_user_id=account_user_id,
    )[0]


def read_shop_product_discoveries(
    job_service: JobService, job_id: str
) -> list[ShopProductDiscovery]:
    """Recover any committed discovery files and return strict SQLite bindings."""

    job = job_service.get(job_id)
    account_user_id = job.input.get("account_user_id")
    if job.type not in {"android_shop_collection", "shop_collection"} or not isinstance(
        account_user_id, str
    ):
        return []
    relative_dir = Path("evidence") / "android" / job_id / "discoveries"
    absolute_dir = _contained_discovery_path(job_service, relative_dir)
    if absolute_dir.exists():
        if absolute_dir.is_symlink() or not absolute_dir.is_dir():
            return []
        for absolute in sorted(absolute_dir.glob("*.json")):
            relative = absolute.relative_to(job_service.runtime_dir)
            loaded = _load_valid_file(
                job_service,
                relative,
                expected_job_id=job_id,
                expected_account_user_id=account_user_id,
            )
            if loaded is None:
                continue
            payload, digest = loaded
            job_service.attach_artifact_once(
                job_id,
                kind=DISCOVERY_ARTIFACT_KIND,
                producer=DISCOVERY_ARTIFACT_PRODUCER,
                path=relative.as_posix(),
                metadata={"schema_version": 1, "sha256": digest, "result": payload},
            )
    return _read_bound_discoveries(job_service, job_id)


def _read_bound_discoveries(
    job_service: JobService,
    job_id: str,
    *,
    expected_path: Path | None = None,
    expected_account_user_id: str | None = None,
) -> list[ShopProductDiscovery]:
    job = job_service.get(job_id)
    account_user_id = expected_account_user_id or job.input.get("account_user_id")
    if not isinstance(account_user_id, str):
        return []
    with job_service.database.session() as session:
        records = session.scalars(
            select(JobArtifactRecord)
            .where(
                JobArtifactRecord.job_id == job_id,
                JobArtifactRecord.kind == DISCOVERY_ARTIFACT_KIND,
            )
            .order_by(JobArtifactRecord.id)
        ).all()
        values: list[ShopProductDiscovery] = []
        for record in records:
            relative = Path(record.path)
            if expected_path is not None and relative != expected_path:
                continue
            loaded = _load_valid_file(
                job_service,
                relative,
                expected_job_id=job_id,
                expected_account_user_id=account_user_id,
            )
            metadata = dict(record.metadata_json)
            if (
                record.producer != DISCOVERY_ARTIFACT_PRODUCER
                or loaded is None
                or metadata.get("schema_version") != 1
            ):
                continue
            payload, digest = loaded
            if metadata.get("sha256") != digest or metadata.get("result") != payload:
                continue
            values.append(_as_discovery(record.id, record.path, digest, payload))
    values.sort(key=lambda value: value.discovery_order)
    if len({value.discovery_order for value in values}) != len(values):
        return []
    return values


def _load_valid_file(
    job_service: JobService,
    relative: Path,
    *,
    expected_job_id: str,
    expected_account_user_id: str,
) -> tuple[dict[str, Any], str] | None:
    match = _DISCOVERY_FILE.fullmatch(relative.name)
    expected_parent = Path("evidence") / "android" / expected_job_id / "discoveries"
    if match is None or relative.parent != expected_parent:
        return None
    absolute = _contained_discovery_path(job_service, relative)
    try:
        if absolute.is_symlink():
            return None
        opened = absolute.open("rb")
        with opened:
            opened_stat = os.fstat(opened.fileno())
            if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_size > 1_000_000:
                return None
            encoded = opened.read(1_000_001)
        payload = json.loads(encoded.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    if not _valid_payload(
        payload,
        expected_job_id=expected_job_id,
        expected_account_user_id=expected_account_user_id,
    ):
        return None
    order = payload["discovery_order"]
    identity = payload["product_stable_identity"]
    if int(match.group(1)) != order or match.group(2) != identity[:16]:
        return None
    if _canonical_bytes(payload) != encoded:
        return None
    return payload, hashlib.sha256(encoded).hexdigest()


def _valid_payload(
    payload: object,
    *,
    expected_job_id: str,
    expected_account_user_id: str,
) -> bool:
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        return False
    visible = payload.get("visible_metadata")
    references = payload.get("raw_evidence_references")
    order = payload.get("discovery_order")
    return (
        payload.get("schema_version") == 1
        and payload.get("discovery_job_id") == expected_job_id
        and payload.get("account_user_id") == expected_account_user_id
        and isinstance(payload.get("product_stable_identity"), str)
        and re.fullmatch(r"[0-9a-f]{64}", payload["product_stable_identity"])
        is not None
        and payload.get("identity_basis") == "source_url_sha256_v1"
        and isinstance(payload.get("source_url"), str)
        and payload["source_url"].startswith("https://")
        and payload.get("canonical_url") is None
        and isinstance(payload.get("title"), str)
        and bool(payload["title"])
        and isinstance(visible, dict)
        and all(isinstance(key, str) and isinstance(value, str) for key, value in visible.items())
        and not isinstance(order, bool)
        and isinstance(order, int)
        and 1 <= order <= 9999
        and isinstance(payload.get("discovered_at"), str)
        and isinstance(references, list)
        and bool(references)
        and len(references) == len(set(references))
        and all(isinstance(value, str) and value for value in references)
    )


def _as_discovery(
    artifact_id: int, artifact_path: str, digest: str, payload: dict[str, Any]
) -> ShopProductDiscovery:
    return ShopProductDiscovery(
        artifact_id=artifact_id,
        discovery_job_id=payload["discovery_job_id"],
        account_user_id=payload["account_user_id"],
        product_stable_identity=payload["product_stable_identity"],
        identity_basis=payload["identity_basis"],
        source_url=payload["source_url"],
        canonical_url=payload["canonical_url"],
        title=payload["title"],
        visible_metadata=dict(payload["visible_metadata"]),
        discovery_order=payload["discovery_order"],
        discovered_at=payload["discovered_at"],
        raw_evidence_references=tuple(payload["raw_evidence_references"]),
        artifact_path=artifact_path,
        result_sha256=digest,
    )


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _contained_discovery_path(job_service: JobService, relative: Path) -> Path:
    candidate = (job_service.runtime_dir / relative).resolve()
    candidate.relative_to(job_service.runtime_dir)
    return candidate


def _write_once(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != encoded:
            raise ValueError("Conflicting Android discovery result already exists.")
        return
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temp.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    # A failed replace deliberately retains the uniquely named temporary file;
    # restart recovery ignores it and no accepted discovery file is deleted.
    temp.replace(path)
