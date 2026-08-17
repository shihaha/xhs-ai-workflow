from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.app.features.shops.service import verify_shop_collection


def _write_product(
    account_dir: Path,
    product_dir: str,
    source_url: str,
    *,
    detail_link: str | None = None,
    manifest_sha256: str | None = None,
) -> None:
    directory = account_dir / product_dir
    images = directory / "images"
    images.mkdir(parents=True)
    image = images / "00.webp"
    image.write_bytes(f"image:{product_dir}".encode("utf-8"))
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    detail = {
        "link": detail_link if detail_link is not None else source_url,
        "image_manifest": [
            {
                "file": "images/00.webp",
                "sha256": manifest_sha256 if manifest_sha256 is not None else digest,
            }
        ],
    }
    (directory / "detail.json").write_text(
        json.dumps(detail, ensure_ascii=False), encoding="utf-8"
    )


def _write_collection(account_dir: Path, products: list[dict[str, str]]) -> None:
    (account_dir / "collection.json").write_text(
        json.dumps(
            {
                "unique_product_link_count": len(products),
                "products": products,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_nn_verification_requires_unique_urls_dirs_matching_details_and_hashes(
    tmp_path: Path,
) -> None:
    """Two distinct fully evidenced products are the only valid complete 2/2 result."""
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    products = [
        {
            "source_url": "https://xhslink.com/product-a",
            "product_dir": "01_product-a",
        },
        {
            "source_url": "https://xhslink.com/product-b",
            "product_dir": "02_product-b",
        },
    ]
    for product in products:
        _write_product(account_dir, product["product_dir"], product["source_url"])
    _write_collection(account_dir, products)

    result = verify_shop_collection(account_dir, expected_count=2)

    assert result.expected_count == 2
    assert result.discovered_count == 2
    assert result.succeeded_count == 2
    assert result.missing_count == 0
    assert result.missing_items == []
    assert result.complete is True


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [("detail_link", "detail_url_mismatch"), ("hash", "image_hash_mismatch")],
)
def test_nn_verification_reports_partial_product_evidence(
    tmp_path: Path, mutation: str, reason: str
) -> None:
    """A bad detail link or image digest remains a named missing product in the N/N facts."""
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    products = [
        {
            "source_url": "https://xhslink.com/product-a",
            "product_dir": "01_product-a",
        },
        {
            "source_url": "https://xhslink.com/product-b",
            "product_dir": "02_product-b",
        },
    ]
    _write_product(account_dir, "01_product-a", products[0]["source_url"])
    _write_product(
        account_dir,
        "02_product-b",
        products[1]["source_url"],
        detail_link=(
            "https://xhslink.com/different"
            if mutation == "detail_link"
            else None
        ),
        manifest_sha256="0" * 64 if mutation == "hash" else None,
    )
    _write_collection(account_dir, products)

    result = verify_shop_collection(account_dir, expected_count=2)

    assert result.expected_count == 2
    assert result.discovered_count == 2
    assert result.succeeded_count == 1
    assert result.missing_count == 1
    assert result.missing_items[0].product_dir == "02_product-b"
    assert result.missing_items[0].reason == reason
    assert result.complete is False


@pytest.mark.parametrize(
    ("duplicate_field", "reason"),
    [
        ("source_url", "duplicate_source_url"),
        ("product_dir", "duplicate_product_dir"),
    ],
)
def test_nn_verification_rejects_duplicate_source_urls_or_product_dirs(
    tmp_path: Path, duplicate_field: str, reason: str
) -> None:
    """A repeated source identity or filesystem target cannot satisfy a second product."""
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    first = {
        "source_url": "https://xhslink.com/product-a",
        "product_dir": "01_product-a",
    }
    second = {
        "source_url": "https://xhslink.com/product-b",
        "product_dir": "02_product-b",
    }
    second[duplicate_field] = first[duplicate_field]
    _write_product(account_dir, first["product_dir"], first["source_url"])
    if second["product_dir"] != first["product_dir"]:
        _write_product(account_dir, second["product_dir"], second["source_url"])
    _write_collection(account_dir, [first, second])

    result = verify_shop_collection(account_dir, expected_count=2)

    assert result.discovered_count == 2
    assert result.succeeded_count == 1
    assert result.missing_count == 1
    assert result.missing_items[0].reason == reason
    assert result.complete is False


def test_nn_verification_lists_undiscovered_expected_slots(tmp_path: Path) -> None:
    """One discovered success from expected two explicitly names the undiscovered slot."""
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    product = {
        "source_url": "https://xhslink.com/product-a",
        "product_dir": "01_product-a",
    }
    _write_product(account_dir, product["product_dir"], product["source_url"])
    _write_collection(account_dir, [product])

    result = verify_shop_collection(account_dir, expected_count=2)

    assert result.expected_count == 2
    assert result.discovered_count == 1
    assert result.succeeded_count == 1
    assert result.missing_count == 1
    assert result.missing_items[0].reference == "expected_product:2"
    assert result.missing_items[0].reason == "expected_product_not_discovered"
    assert result.complete is False


def test_nn_verification_rejects_product_directory_escape(tmp_path: Path) -> None:
    """A manifest row cannot make verification read a product outside the account directory."""
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "detail.json").write_text("{}", encoding="utf-8")
    _write_collection(
        account_dir,
        [
            {
                "source_url": "https://xhslink.com/product-a",
                "product_dir": "../outside",
            }
        ],
    )

    result = verify_shop_collection(account_dir, expected_count=1)

    assert result.succeeded_count == 0
    assert result.missing_count == 1
    assert result.missing_items[0].reason == "invalid_product_dir"
    assert result.complete is False
