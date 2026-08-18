from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from backend.app.db import Database
from backend.app.models.jobs import JobArtifactRecord, JobState
from backend.app.services.jobs import JobService


def _pending_result(jobs: JobService, runtime_dir: Path) -> tuple[str, Path, Path]:
    job = jobs.create(job_type="android_shop_collection", input_data={})
    jobs.claim(job.id)
    result_dir = runtime_dir / "evidence" / "shops" / job.id
    result_dir.mkdir(parents=True)
    temp = result_dir / ".result-release.tmp"
    final = result_dir / "result.json"
    temp.write_text('{"status":"succeeded"}\n', encoding="utf-8")
    return job.id, temp, final


def _finalize(jobs: JobService, job_id: str, temp: Path, final: Path) -> None:
    jobs.finalize_running_with_artifact(
        job_id,
        state=JobState.succeeded,
        progress_current=1,
        progress_total=1,
        current_stage="shop_complete",
        error_category=None,
        kind="shop_collection_result",
        temp_path=temp.relative_to(jobs.runtime_dir).as_posix(),
        path=final.relative_to(jobs.runtime_dir).as_posix(),
        metadata={"result": {"status": "succeeded"}},
    )


def test_job_result_bytes_are_retained_when_commit_fails_before_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database failure must not permanently erase the only generated evidence."""
    runtime = tmp_path / "runtime"
    jobs = JobService(Database(runtime / "db.sqlite3"), runtime_dir=runtime)
    job_id, temp, final = _pending_result(jobs, runtime)
    original_commit = Session.commit

    def fail_result_commit(session: Session) -> None:
        if any(isinstance(value, JobArtifactRecord) for value in session.new):
            raise RuntimeError("simulated database outage")
        original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_result_commit)
    with pytest.raises(RuntimeError, match="database outage"):
        _finalize(jobs, job_id, temp, final)

    assert final.read_text(encoding="utf-8") == '{"status":"succeeded"}\n'


def test_job_result_bytes_are_retained_when_commit_lands_but_ack_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambiguous commit acknowledgement must retain the referenced final file."""
    runtime = tmp_path / "runtime"
    jobs = JobService(Database(runtime / "db.sqlite3"), runtime_dir=runtime)
    job_id, temp, final = _pending_result(jobs, runtime)
    original_commit = Session.commit

    def commit_then_raise(session: Session) -> None:
        if any(isinstance(value, JobArtifactRecord) for value in session.new):
            original_commit(session)
            raise RuntimeError("simulated lost commit acknowledgement")
        original_commit(session)

    monkeypatch.setattr(Session, "commit", commit_then_raise)
    with pytest.raises(RuntimeError, match="lost commit acknowledgement"):
        _finalize(jobs, job_id, temp, final)

    assert final.read_text(encoding="utf-8") == '{"status":"succeeded"}\n'
    durable = jobs.get(job_id)
    assert durable.state is JobState.succeeded
    assert durable.artifacts[0].path == final.relative_to(runtime).as_posix()


def test_cancelled_job_retains_unclaimed_temp_evidence(tmp_path: Path) -> None:
    """Losing the state CAS must retain bytes for operator recovery, not unlink them."""
    runtime = tmp_path / "runtime"
    jobs = JobService(Database(runtime / "db.sqlite3"), runtime_dir=runtime)
    job_id, temp, final = _pending_result(jobs, runtime)
    jobs.transition(job_id, JobState.cancelled)

    result = _finalize(jobs, job_id, temp, final)

    assert result is None
    assert temp.read_text(encoding="utf-8") == '{"status":"succeeded"}\n'
    assert not final.exists()
