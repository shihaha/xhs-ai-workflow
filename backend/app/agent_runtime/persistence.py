"""SQLite persistence for the isolated Agent Runtime spike."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.agent_runtime.types import AgentRunState, ModelTurn, RunBudget, RunUsage
from backend.app.db import Base, Database


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentRunRecord(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    final_output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentStepRecord(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("run_id", "step_index", name="uq_agent_step_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence_refs_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PermissionDecisionRecord(Base):
    __tablename__ = "permission_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), nullable=False, index=True)
    tool_call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class HumanActionRecord(Base):
    __tablename__ = "human_actions"
    __table_args__ = (UniqueConstraint("run_id", "tool_call_id", name="uq_human_action_call"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), nullable=False, index=True)
    tool_call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    resolution_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentCheckpointRecord(Base):
    __tablename__ = "agent_checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "step_index", name="uq_agent_checkpoint_step"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AgentRunStore:
    """Small durable repository; explicit table creation keeps the spike isolated."""

    _TABLES = (
        AgentRunRecord.__table__,
        AgentStepRecord.__table__,
        PermissionDecisionRecord.__table__,
        HumanActionRecord.__table__,
        AgentCheckpointRecord.__table__,
    )

    def __init__(self, database: Database) -> None:
        self.database = database
        for table in self._TABLES:
            table.create(bind=database.engine, checkfirst=True)

    def create_run(
        self,
        *,
        goal: str,
        budget: RunBudget,
        model_name: str | None = None,
        prompt_version: str | None = None,
    ) -> AgentRunRecord:
        now = _utcnow()
        record = AgentRunRecord(
            id=str(uuid4()),
            goal=goal,
            state=AgentRunState.running.value,
            model_name=model_name,
            prompt_version=prompt_version,
            budget_json=budget.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
        with self.database.sessions.begin() as session:
            session.add(record)
        return record

    def get_run(self, run_id: str) -> AgentRunRecord:
        with self.database.sessions() as session:
            record = session.get(AgentRunRecord, run_id)
            if record is None:
                raise KeyError(f"agent run not found: {run_id}")
            session.expunge(record)
            return record

    def list_steps(self, run_id: str) -> list[AgentStepRecord]:
        with self.database.sessions() as session:
            records = list(
                session.scalars(
                    select(AgentStepRecord)
                    .where(AgentStepRecord.run_id == run_id)
                    .order_by(AgentStepRecord.step_index)
                )
            )
            for record in records:
                session.expunge(record)
            return records

    def append_step(
        self,
        run_id: str,
        *,
        kind: str,
        status: str,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        input_json: dict[str, Any] | None = None,
        output_json: dict[str, Any] | None = None,
        evidence_refs: list[str] | None = None,
        error_category: str | None = None,
        error_detail: str | None = None,
    ) -> AgentStepRecord:
        now = _utcnow()
        with self.database.sessions.begin() as session:
            run = session.get(AgentRunRecord, run_id)
            if run is None:
                raise KeyError(f"agent run not found: {run_id}")
            next_index = run.step_count + 1
            record = AgentStepRecord(
                run_id=run_id,
                step_index=next_index,
                kind=kind,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                input_json=input_json,
                output_json=output_json,
                evidence_refs_json=evidence_refs or [],
                status=status,
                error_category=error_category,
                error_detail=error_detail,
                created_at=now,
                updated_at=now,
            )
            run.step_count = next_index
            run.updated_at = now
            session.add(record)
        return record

    def update_step(
        self,
        step_id: int,
        *,
        status: str,
        output_json: dict[str, Any] | None = None,
        evidence_refs: list[str] | None = None,
        error_category: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        with self.database.sessions.begin() as session:
            step = session.get(AgentStepRecord, step_id)
            if step is None:
                raise KeyError(f"agent step not found: {step_id}")
            step.status = status
            step.output_json = output_json
            if evidence_refs is not None:
                step.evidence_refs_json = evidence_refs
            step.error_category = error_category
            step.error_detail = error_detail
            step.updated_at = _utcnow()

    def add_model_usage(self, run_id: str, turn: ModelTurn) -> None:
        with self.database.sessions.begin() as session:
            run = session.get(AgentRunRecord, run_id)
            if run is None:
                raise KeyError(f"agent run not found: {run_id}")
            run.model_calls += 1
            run.input_tokens += turn.input_tokens
            run.output_tokens += turn.output_tokens
            if turn.model_name:
                run.model_name = turn.model_name
            run.updated_at = _utcnow()

    def record_permission(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        decision: str,
        reason: str,
    ) -> None:
        with self.database.sessions.begin() as session:
            session.add(
                PermissionDecisionRecord(
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    decision=decision,
                    reason=reason,
                    created_at=_utcnow(),
                )
            )

    def create_human_action(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        request_json: dict[str, Any],
    ) -> HumanActionRecord:
        record = HumanActionRecord(
            id=str(uuid4()),
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status="pending",
            request_json=request_json,
            created_at=_utcnow(),
        )
        with self.database.sessions.begin() as session:
            session.add(record)
        return record

    def pending_human_action(self, run_id: str) -> HumanActionRecord | None:
        with self.database.sessions() as session:
            record = session.scalar(
                select(HumanActionRecord)
                .where(HumanActionRecord.run_id == run_id)
                .where(HumanActionRecord.status == "pending")
                .order_by(HumanActionRecord.created_at.desc())
            )
            if record is not None:
                session.expunge(record)
            return record

    def resolve_human_action(self, action_id: str, *, approved: bool, note: str | None = None) -> HumanActionRecord:
        with self.database.sessions.begin() as session:
            record = session.get(HumanActionRecord, action_id)
            if record is None:
                raise KeyError(f"human action not found: {action_id}")
            if record.status != "pending":
                raise ValueError("human action is already resolved")
            record.status = "approved" if approved else "denied"
            record.resolution_json = {"approved": approved, "note": note}
            record.resolved_at = _utcnow()
            session.flush()
            session.expunge(record)
            return record

    def checkpoint(self, run_id: str, *, state_json: dict[str, Any]) -> AgentCheckpointRecord:
        run = self.get_run(run_id)
        record = AgentCheckpointRecord(
            run_id=run_id,
            step_index=run.step_count,
            state_json=state_json,
            created_at=_utcnow(),
        )
        with self.database.sessions.begin() as session:
            session.add(record)
        return record

    def set_state(
        self,
        run_id: str,
        state: AgentRunState,
        *,
        final_output: dict[str, Any] | None = None,
        error_category: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        now = _utcnow()
        with self.database.sessions.begin() as session:
            run = session.get(AgentRunRecord, run_id)
            if run is None:
                raise KeyError(f"agent run not found: {run_id}")
            run.state = state.value
            run.final_output_json = final_output
            run.error_category = error_category
            run.error_detail = error_detail
            run.updated_at = now
            if state in {AgentRunState.succeeded, AgentRunState.failed, AgentRunState.cancelled}:
                run.completed_at = now
            else:
                run.completed_at = None

    def usage(self, run_id: str) -> RunUsage:
        run = self.get_run(run_id)
        return RunUsage(
            model_calls=run.model_calls,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
        )

    def recover_interrupted(self, run_id: str) -> AgentRunRecord:
        """Fail closed; never replay an uncertain in-flight side effect."""

        run = self.get_run(run_id)
        if run.state != AgentRunState.running.value:
            return run
        steps = self.list_steps(run_id)
        last = steps[-1] if steps else None
        category = "interrupted"
        detail = "Agent run was interrupted and requires explicit resume."
        if last is not None and last.kind == "tool" and last.status == "started":
            category = "uncertain_tool_side_effect"
            detail = "A tool started but no committed result exists; automatic replay is forbidden."
        self.set_state(
            run_id,
            AgentRunState.needs_human,
            error_category=category,
            error_detail=detail,
        )
        return self.get_run(run_id)
