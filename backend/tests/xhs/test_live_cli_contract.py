"""Explicit opt-in smoke for a trusted local xhs-cli read-only session."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select

import backend.app.adapters.xhs_cli_read as cli_module
from backend.app.adapters.xhs_cli_read import XhsCliReadAdapter, XhsCliReadError
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
    return XhsCliReadAdapter.from_settings(settings).probe_session_identity()


def _prepare_external_state(settings: Settings) -> None:
    assert settings.xhs_cli_state_dir is not None
    config_dir = settings.xhs_cli_state_dir / ".xhs-cli"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cookies.json").write_text(
        json.dumps({"cookies": {"a1": "prepared-a1", "web_session": "prepared-session"}}),
        encoding="utf-8",
    )


def test_authenticated_fake_cli_runs_the_exact_read_only_contract(tmp_path, monkeypatch) -> None:
    """Adding --json to status, changing arguments, or invoking a write command must fail."""
    settings = Settings(runtime_dir=tmp_path, database_path=tmp_path / "live.sqlite3")
    _prepare_external_state(settings)
    expected = [
        [str(settings.xhs_cli_executable), "status"],
        [str(settings.xhs_cli_executable), "whoami", "--json"],
        [str(settings.xhs_cli_executable), "user", "user-1", "--json"],
        [str(settings.xhs_cli_executable), "user-posts", "user-1", "--json"],
        [str(settings.xhs_cli_executable), "search", "收纳", "--json"],
    ]
    payloads: list[bytes] = [
        "Logged in (from saved cookies)".encode(),
        json.dumps({
            "userPageData": {"basicInfo": {"userId": "session-user", "nickname": "Operator"}},
            "userInfo": {"userId": "session-user", "guest": False},
        }).encode(),
        json.dumps({
            "userPageData": {
                "basicInfo": {"userId": "user-1", "redId": "red-1", "nickname": "Alice", "desc": "公开简介"},
                "interactions": [{"name": "fans", "count": 12}],
            },
            "userInfo": {"userId": "user-1", "guest": False},
        }).encode(),
        json.dumps([{
            "id": "note-1",
            "xsecToken": "live-fake-xsec-secret",
            "noteCard": {
                "displayTitle": "First",
                "user": {"userId": "user-1", "nickname": "Alice"},
                "interactInfo": {"likedCount": 7},
            },
        }]).encode(),
        json.dumps([{
            "id": "search-1",
            "xsec_token": "live-fake-search-xsec-secret",
            "noteCard": {"displayTitle": "收纳", "user": {"userId": "user-2"}},
        }]).encode(),
    ]
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        index = len(calls)
        assert argv == expected[index]
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=payloads[index], stderr=b"")

    monkeypatch.setattr(cli_module, "_run_bounded_process", fake_run)
    for name, value in {
        "XHS_LIVE_USER_ID": "user-1",
        "XHS_LIVE_KEYWORD": "收纳",
        "XHS_LIVE_EXPECTED_NOTE_COUNT": "1",
        "XHS_LIVE_EXPECTED_SEARCH_COUNT": "1",
        "XHS_LIVE_STATE_DIR": str(settings.xhs_cli_state_dir),
    }.items():
        monkeypatch.setenv(name, value)

    test_live_xhs_cli_account_and_search_contract_opt_in(tmp_path)

    assert [argv for argv, _ in calls] == expected
    assert all(kwargs.get("shell") is False for _, kwargs in calls)
    assert all(Path(str(kwargs["cwd"])).resolve() == settings.xhs_cli_state_dir for _, kwargs in calls)
    assert all("PARENT_SECRET_SENTINEL" not in kwargs["env"] for _, kwargs in calls)
    assert not any(token in {"login", "logout", "post", "delete", "cookie", "token"} for argv, _ in calls for token in argv[1:])


def test_missing_external_state_is_not_run_and_writes_no_facts(tmp_path, monkeypatch) -> None:
    settings = Settings(runtime_dir=tmp_path, database_path=tmp_path / "live.sqlite3")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, stdout=b"Not logged in", stderr=b"")

    monkeypatch.setattr(cli_module, "_run_bounded_process", fake_run)
    for name, value in {
        "XHS_LIVE_USER_ID": "user-1",
        "XHS_LIVE_KEYWORD": "收纳",
        "XHS_LIVE_EXPECTED_NOTE_COUNT": "1",
        "XHS_LIVE_EXPECTED_SEARCH_COUNT": "1",
        "XHS_LIVE_STATE_DIR": str(settings.xhs_cli_state_dir),
    }.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(pytest.skip.Exception, match="not_run"):
        test_live_xhs_cli_account_and_search_contract_opt_in(tmp_path)

    assert calls == []
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
    state_dir = Path(_required("XHS_LIVE_STATE_DIR")).resolve()
    settings = Settings(
        runtime_dir=state_dir.parent,
        database_path=tmp_path / "live.sqlite3",
        xhs_cli_state_dir=state_dir,
    )

    try:
        _trusted_session_identity(settings)
    except (OSError, XhsCliReadError) as error:
        assert not settings.database_path.exists()
        category = error.category if isinstance(error, XhsCliReadError) else type(error).__name__
        pytest.skip(f"not_run: trusted local xhs-cli session unavailable ({category})")

    database = Database(settings.database_path, runtime_dir=tmp_path)
    jobs = JobService(database, runtime_dir=tmp_path)
    service = XhsCollectionService(
        database=database,
        job_service=jobs,
        adapter=XhsCliReadAdapter.from_settings(settings),
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
