from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.db import Database
from backend.app.services.jobs import JobCreateSpec, JobService


def test_job_batch_reservation_rolls_back_every_row_on_mid_batch_invalid_record(
    tmp_path: Path,
) -> None:
    service = JobService(Database(tmp_path / "db.sqlite3"), runtime_dir=tmp_path)

    with pytest.raises(IntegrityError):
        service.create_batch(
            [
                JobCreateSpec(job_type="qianfan_ranking_scope", input_data={}),
                JobCreateSpec(job_type=None, input_data={}),  # type: ignore[arg-type]
                JobCreateSpec(job_type="qianfan_ranking_scope", input_data={}),
            ]
        )

    assert service.list() == []
