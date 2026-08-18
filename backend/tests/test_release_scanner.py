from pathlib import Path

from tools.scan_release_boundaries import scan_python_boundaries


def test_scanner_catches_argument_bearing_permanent_delete_calls(tmp_path: Path) -> None:
    app = tmp_path / "backend" / "app" / "services"
    app.mkdir(parents=True)
    (app / "jobs.py").write_text(
        "from pathlib import Path\n"
        "import os, shutil\n"
        "Path('evidence').unlink(missing_ok=True)\n"
        "Path('evidence').rmdir()\n"
        "os.remove('evidence')\n"
        "os.rmdir('evidence')\n"
        "shutil.rmtree('evidence')\n"
        "content_export.remove_contained_regular('evidence', roots=())\n"
        "content_export._delete_open_file(7)\n"
        "content_export._set_delete_disposition(7, True)\n"
        "kernel32.SetFileInformationByHandle(1, 2, 3, 4)\n",
        encoding="utf-8",
    )

    findings = scan_python_boundaries(tmp_path)

    assert [finding.category for finding in findings] == [
        "permanent_delete",
        "permanent_delete",
        "permanent_delete",
        "permanent_delete",
        "permanent_delete",
        "permanent_delete",
        "permanent_delete",
        "permanent_delete",
        "permanent_delete",
    ]


def test_scanner_allows_only_the_reviewed_cleanup_delete_boundary(
    tmp_path: Path,
) -> None:
    cleanup = tmp_path / "backend" / "app" / "features" / "content"
    cleanup.mkdir(parents=True)
    (cleanup / "cleanup.py").write_text(
        "from pathlib import Path\nPath('quarantine').unlink(missing_ok=True)\n",
        encoding="utf-8",
    )

    assert scan_python_boundaries(tmp_path) == []


def test_scanner_allows_native_delete_implementation_only_in_export_module(
    tmp_path: Path,
) -> None:
    export = tmp_path / "backend" / "app" / "features" / "content"
    export.mkdir(parents=True)
    (export / "export.py").write_text(
        "def _delete_open_file(fd):\n"
        "    return kernel32.SetFileInformationByHandle(fd, 1, 2, 3)\n",
        encoding="utf-8",
    )

    assert scan_python_boundaries(tmp_path) == []


def test_scanner_allows_only_the_reviewed_xhs_staging_delete_boundary(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "backend" / "app" / "features" / "xhs"
    staging.mkdir(parents=True)
    (staging / "staging_cleanup.py").write_text(
        "import os\nos.unlink('one-private.stage')\n",
        encoding="utf-8",
    )

    assert scan_python_boundaries(tmp_path) == []


def test_scanner_catches_destructive_http_route(tmp_path: Path) -> None:
    api = tmp_path / "backend" / "app" / "api"
    api.mkdir(parents=True)
    (api / "unsafe.py").write_text(
        "@router.delete('/evidence')\ndef destroy():\n    pass\n",
        encoding="utf-8",
    )

    findings = scan_python_boundaries(tmp_path)

    assert len(findings) == 1
    assert findings[0].category == "destructive_http_route"
