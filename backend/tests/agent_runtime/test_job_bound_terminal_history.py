from pathlib import Path

from backend.app.agent_runtime import AgentRunState, AgentRunStore, RunBudget
from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.agent_runtime.job_terminal_projection import JobTerminalProjector
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


def test_evidence_requirement_uses_prior_continuation_run_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    jobs = JobService(database)
    job = jobs.create(
        job_type="agent_orchestration",
        input_data={"agent_completion": {"require_evidence_refs": True}},
    )
    run_ids = iter(("run-before-wait", "run-after-wait"))
    coordinator = AgentJobCoordinator(database, run_id_factory=run_ids.__next__)
    first = coordinator.claim_and_create_run(
        job.id,
        goal="collect evidence before approval",
        budget=RunBudget(max_wall_time_seconds=30),
    )
    store = AgentRunStore(database)
    store.append_step(
        first.run_id,
        kind="tool",
        status="succeeded",
        tool_name="evidence.read",
        tool_call_id="evidence-before-wait",
        input_json={"arguments": {}},
        output_json={"fact": "durable historical evidence"},
        evidence_refs=["evidence:history:1"],
    )
    store.set_state(
        first.run_id,
        AgentRunState.needs_human,
        error_category="approval_required",
        error_detail="continue after human decision",
    )
    jobs.transition(job.id, JobState.needs_human)

    second = coordinator.claim_and_create_run(
        job.id,
        goal="finish using already collected evidence",
        budget=RunBudget(max_wall_time_seconds=30),
    )
    final_output = {"ok": True, "source": "historical evidence"}
    store.append_step(
        second.run_id,
        kind="output",
        status="succeeded",
        output_json=final_output,
    )
    store.set_state(
        second.run_id,
        AgentRunState.succeeded,
        final_output=final_output,
    )

    result = JobTerminalProjector(database).project_terminal(second.run_id)

    assert result.projected is True
    assert result.evidence_refs == ("evidence:history:1",)
    assert jobs.get(job.id).state is JobState.succeeded
