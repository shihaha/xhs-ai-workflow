from pathlib import Path

from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.db import Base, Database


def test_agent_job_binding_table_stays_out_of_production_metadata(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    AgentJobCoordinator(database)

    assert "agent_job_bindings" not in Base.metadata.tables
