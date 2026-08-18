"""Explicit opt-in smoke for a trusted local xhs-cli read-only session."""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest
from sqlalchemy import func, select

from backend.app.adapters.xhs_cli_read import XhsCliReadAdapter, XhsCliReadError, decode_bounded_json
from backend.app.db import Database
from backend.app.features.xhs.models import XhsAccountNoteRecord, XhsAccountProfileRecord
from backend.app.features.xhs.service import XhsCollectionService
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService
from backend.app.settings import Settings


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"not_run: {name} is required for the opt-in live gate")
    return value


def _count(name: str) -> int:
    value = _required(name)
    try:
        parsed = int(value)
    except ValueError:
        pytest.fail(f"{name} must be an integer from 0 through 1000")
    if not 0 <= parsed <= 1000:
        pytest.fail(f"{name} must be an integer from 0 through 1000")
    return parsed


def _trusted_session_identity(settings: Settings) -> str:
    status = subprocess.run(
        [str(settings.xhs_cli_executable), "status"],
        shell=False,
        capture_output=True,
        timeout=settings.xhs_cli_timeout_seconds,
        check=False,
    )
    if not isinstance(status.stdout, bytes) or not isinstance(status.stderr, bytes):
        raise XhsCliReadError("malformed_output")
    if len(status.stdout) > 64 * 1024 or len(status.stderr) > 64 * 1024:
        raise XhsCliReadError("output_too_large")
    if status.returncode != 0:
        raise XhsCliReadError("login_required")
    try:
        status_text = (status.stdout + b"\n" + status.stderr).decode("utf-8", errors="strict").casefold()
    except UnicodeDecodeError as error:
        raise XhsCliReadError("malformed_output") from error
    if "not logged in" in status_text or "logged in" not in status_text:
        raise XhsCliReadError("login_required")

    whoami = subprocess.run(
        [str(settings.xhs_cli_executable), "whoami", "--json"],
        shell=False,
        capture_output=True,
        timeout=settings.xhs_cli_timeout_seconds,
        check=False,
    )
    payload = decode_bounded_json(whoami)
    candidates = [payload]
    for key in ("userInfo", "basicInfo", "basic_info"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            candidates.append(candidate)
    user_page = payload.get("userPageData")
    if isinstance(user_page, dict):
        for key in ("basicInfo", "basic_info"):
            candidate = user_page.get(key)
            if isinstance(candidate, dict):
                candidates.append(candidate)
    for candidate in candidates:
        for key in ("userId", "user_id", "id"):
            value = candidate.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                identity = str(value).strip()
                if re.fullmatch(r"[A-Za-z0-9_-]{1,500}", identity, re.ASCII):
                    return identity
    raise XhsCliReadError("login_required")


def test_authenticated_fake_cli_runs_the_exact_read_only_contract(tmp_path, monkeypatch) -> None:
    """Adding --json to status, changing arguments, or invoking a write command must fail."""
    settings = Settings(runtime_dir=tmp_path, database_path=tmp_path / "live.sqlite3")
    expected = [
        [str(settings.xhs_cli_executable), "status"],
        [str(settings.xhs_cli_executable), "whoami", "--json"],
        [str(settings.xhs_cli_executable), "user", "user-1", "--json"],
        [str(settings.xhs_cli_executable), "user-posts", "user-1", "--json"],
        [str(settings.xhs_cli_executable), "search", "收纳", "--json"],
    ]
    payloads: list[bytes] = [
        "Logged in (from saved cookies)".encode(),
        json.dumps({"userInfo": {"userId": "session-user"}}).encode(),
        json.dumps({"user": {"id": "user-1", "nickname": "Alice"}}).encode(),
        json.dumps({"notes": [{"id": "note-1", "title": "First", "user_id": "user-1"}]}).encode(),
        json.dumps({"notes": [{"id": "search-1", "title": "收纳"}]}).encode(),
    ]
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        index = len(calls)
        assert argv == expected[index]
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=payloads[index], stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    for name, value in {
        "XHS_LIVE_USER_ID": "user-1",
        "XHS_LIVE_KEYWORD": "收纳",
        "XHS_LIVE_EXPECTED_NOTE_COUNT": "1",
        "XHS_LIVE_EXPECTED_SEARCH_COUNT": "1",
    }.items():
        monkeypatch.setenv(name, value)

    test_live_xhs_cli_account_and_search_contract_opt_in(tmp_path)

    assert [argv for argv, _ in calls] == expected
    assert all(kwargs.get("shell") is False for _, kwargs in calls)
    assert not any(token in {"login", "logout", "post", "delete", "cookie", "token"} for argv, _ in calls for token in argv[1:])


def test_unauthenticated_fake_cli_is_not_run_and_writes_no_facts(tmp_path, monkeypatch) -> None:
    settings = Settings(runtime_dir=tmp_path, database_path=tmp_path / "live.sqlite3")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, stdout=b"Not logged in", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    for name, value in {
        "XHS_LIVE_USER_ID": "user-1",
        "XHS_LIVE_KEYWORD": "收纳",
        "XHS_LIVE_EXPECTED_NOTE_COUNT": "1",
        "XHS_LIVE_EXPECTED_SEARCH_COUNT": "1",
    }.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(pytest.skip.Exception, match="not_run"):
        test_live_xhs_cli_account_and_search_contract_opt_in(tmp_path)

    assert calls == [[str(settings.xhs_cli_executable), "status"]]
    assert not settings.database_path.exists()


@pytest.mark.skipif(
    os.environ.get("XHS_LIVE_TEST") != "1",
    reason="not_run: XHS_LIVE_TEST=1 was not supplied",
)
def test_live_xhs_cli_account_and_search_contract_opt_in(tmp_path) -> None:
    """Use only fixed read commands; never log in, mutate platform state, or accept credentials."""
    user_id = _required("XHS_LIVE_USER_ID")
    keyword = _required("XHS_LIVE_KEYWORD")
    expected_notes = _count("XHS_LIVE_EXPECTED_NOTE_COUNT")
    expected_search = _count("XHS_LIVE_EXPECTED_SEARCH_COUNT")
    settings = Settings(runtime_dir=tmp_path, database_path=tmp_path / "live.sqlite3")

    try:
        _trusted_session_identity(settings)
    except (OSError, subprocess.TimeoutExpired, XhsCliReadError) as error:
        assert not settings.database_path.exists()
        category = error.category if isinstance(error, XhsCliReadError) else type(error).__name__
        pytest.skip(f"not_run: trusted local xhs-cli session unavailable ({category})")

    database = Database(settings.database_path, runtime_dir=tmp_path)
    jobs = JobService(database, runtime_dir=tmp_path)
    service = XhsCollectionService(
        database=database,
        job_service=jobs,
        adapter=XhsCliReadAdapter.from_settings(settings, runner=subprocess.run),
        runtime_dir=tmp_path,
        submitter=lambda *_args: None,
    )
    account_job = service.submit_account(user_id, expected_notes)
    account_result = service.execute(account_job.id)
    assert account_result is not None
    if account_result.state is JobState.needs_human:
        with database.session() as session:
            assert session.scalar(select(func.count()).select_from(XhsAccountProfileRecord)) == 0
            assert session.scalar(select(func.count()).select_from(XhsAccountNoteRecord)) == 0
        pytest.skip(f"needs_human: {account_result.error_category}")
    assert account_result.state is JobState.succeeded
    assert len(service.list_account_notes(user_id)) == expected_notes

    search_job = service.submit_search(keyword, expected_search)
    search_result = service.execute(search_job.id)
    assert search_result is not None
    if search_result.state is JobState.needs_human:
        pytest.skip(f"needs_human: {search_result.error_category}")
    assert search_result.state is JobState.succeeded
    assert service.get_search_results(search_job.id).succeeded_count == expected_search
