"""Explicit opt-in smoke for a trusted local xhs-cli read-only session."""

from __future__ import annotations

import os
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
        for command in ("status", "whoami"):
            completed = subprocess.run(
                [settings.xhs_cli_executable, command, "--json"],
                shell=False,
                capture_output=True,
                timeout=settings.xhs_cli_timeout_seconds,
                check=False,
            )
            decode_bounded_json(completed)
    except (OSError, subprocess.TimeoutExpired, XhsCliReadError) as error:
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
