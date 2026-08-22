from pathlib import Path

from sqlalchemy import inspect

from backend.app.agent_runtime import AgentRunState, AgentRunStore, RunBudget
from backend.app.db import Database


def test_spike_tables_share_existing_sqlite_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    AgentRunStore(database)

    tables = set(inspect(database.engine).get_table_names())

    assert {
        "agent_runs",
        "agent_steps",
        "permission_decisions",
        "human_actions",
        "agent_checkpoints",
    }.issubset(tables)
    assert "jobs" in tables


def test_interrupted_started_tool_never_auto_replays(tmp_path: Path) -> None:
    store = AgentRunStore(Database(tmp_path / "workbench.sqlite3"))
    run = store.create_run(goal="simulate crash", budget=RunBudget())
    store.append_step(
        run.id,
        kind="tool",
        status="started",
        tool_name="external.simulated_action",
        tool_call_id="call-1",
        input_json={"arguments": {"value": 1}, "idempotent": False},
    )

    recovered = store.recover_interrupted(run.id)

    assert recovered.state == AgentRunState.needs_human.value
    assert recovered.error_category == "uncertain_tool_side_effect"
    assert store.list_steps(run.id)[-1].status == "started"


def test_interrupted_run_without_started_side_effect_requires_human(tmp_path: Path) -> None:
    store = AgentRunStore(Database(tmp_path / "workbench.sqlite3"))
    run = store.create_run(goal="simulate ordinary interruption", budget=RunBudget())

    recovered = store.recover_interrupted(run.id)

    assert recovered.state == AgentRunState.needs_human.value
    assert recovered.error_category == "interrupted"
