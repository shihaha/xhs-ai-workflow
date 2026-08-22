"""Bind tutorial benchmark research to the existing trusted XHS search pipeline."""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
from threading import RLock

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app.db import Database
from backend.app.features.content_research.benchmark_schemas import (
    BenchmarkNoteRead,
    BenchmarkOverviewRead,
    BenchmarkSearchCreate,
    BenchmarkSearchNoteRead,
    BenchmarkSearchRead,
)
from backend.app.features.content_research.models import (
    BenchmarkSearchRecord,
    FinishedProductDossierRecord,
    KeywordPlanRecord,
    KeywordPlanRunRecord,
)
from backend.app.features.xhs.service import (
    CollectionFactNotFound,
    XhsCollectionService,
)
from backend.app.models.jobs import JobState
from backend.app.services.jobs import InvalidJobTransition, JobNotFound


class BenchmarkResearchError(RuntimeError):
    pass


class BenchmarkResearchNotFound(BenchmarkResearchError):
    pass


class BenchmarkResearchConflict(BenchmarkResearchError):
    pass


class BenchmarkResearchUnavailable(BenchmarkResearchError):
    pass


class BenchmarkResearchService:
    def __init__(
        self, database: Database, xhs_service: XhsCollectionService | None
    ) -> None:
        self.database = database
        self.xhs_service = xhs_service
        self._admission_lock = RLock()

    def start_search(
        self, dossier_id: str, payload: BenchmarkSearchCreate
    ) -> BenchmarkSearchRead:
        service = self._require_xhs()
        with self._admission_lock:
            item, run = self._current_keyword_item(
                dossier_id, payload.keyword_item_id
            )
            existing = self._latest_attempt(item.id, payload.stage)
            if existing is not None:
                try:
                    job = service.job_service.get(existing.xhs_job_id)
                except JobNotFound as error:
                    raise BenchmarkResearchConflict(
                        "Persisted benchmark binding points to a missing XHS job."
                    ) from error
                if job.state not in {JobState.failed, JobState.cancelled}:
                    return self._project(existing)

            if payload.stage == "full":
                self._require_trusted_probe(item.id)
                expected_count = item.target_count
            else:
                expected_count = 2

            attempt = (existing.attempt + 1) if existing is not None else 1
            job = service.submit_search(item.keyword, expected_count)
            record = BenchmarkSearchRecord(
                dossier_id=dossier_id,
                keyword_run_id=run.id,
                keyword_item_id=item.id,
                stage=payload.stage,
                attempt=attempt,
                keyword=item.keyword,
                expected_count=expected_count,
                xhs_job_id=job.id,
                created_at=_now(),
            )
            try:
                with self.database.session() as session:
                    session.add(record)
                    session.commit()
                    session.refresh(record)
            except IntegrityError as error:
                self._cancel_unbound_job(job.id)
                raise BenchmarkResearchConflict(
                    "Benchmark search admission raced with another request."
                ) from error
            return self._project(record)

    def overview(self, dossier_id: str) -> BenchmarkOverviewRead:
        self._require_dossier(dossier_id)
        current_run_id = self._current_run_id(dossier_id)
        with self.database.session() as session:
            records = session.scalars(
                select(BenchmarkSearchRecord)
                .where(BenchmarkSearchRecord.dossier_id == dossier_id)
                .order_by(
                    BenchmarkSearchRecord.created_at,
                    BenchmarkSearchRecord.id,
                )
            ).all()
            for record in records:
                session.expunge(record)

        searches = [self._project(record) for record in records]
        deduped: OrderedDict[str, dict[str, object]] = OrderedDict()
        for search in searches:
            if search.stage != "full" or search.result_status != "trusted":
                continue
            for note in search.items:
                key = note.note_id or note.source_url
                current = deduped.get(key)
                if current is None:
                    deduped[key] = {
                        "note_id": note.note_id,
                        "source_url": note.source_url,
                        "title": note.title,
                        "summary": note.summary,
                        "user_id": note.user_id,
                        "source_search_ids": [search.id],
                        "source_keyword_item_ids": [search.keyword_item_id],
                        "source_keywords": [search.keyword],
                    }
                    continue
                _append_unique(current["source_search_ids"], search.id)
                _append_unique(
                    current["source_keyword_item_ids"], search.keyword_item_id
                )
                _append_unique(current["source_keywords"], search.keyword)

        unique_notes = [BenchmarkNoteRead.model_validate(item) for item in deduped.values()]
        return BenchmarkOverviewRead(
            dossier_id=dossier_id,
            current_keyword_run_id=current_run_id,
            searches=searches,
            unique_full_notes=unique_notes,
            unique_full_note_count=len(unique_notes),
        )

    def _project(self, record: BenchmarkSearchRecord) -> BenchmarkSearchRead:
        service = self._require_xhs()
        try:
            job = service.job_service.get(record.xhs_job_id)
        except JobNotFound as error:
            raise BenchmarkResearchConflict(
                "Persisted benchmark binding points to a missing XHS job."
            ) from error

        result_status = "pending"
        succeeded_count = None
        artifact_id = None
        collected_at = None
        items: list[BenchmarkSearchNoteRead] = []
        if job.state is JobState.succeeded:
            try:
                result = service.get_search_results(job.id)
            except CollectionFactNotFound:
                result_status = "untrusted"
            else:
                if (
                    result.keyword != record.keyword
                    or result.expected_count != record.expected_count
                    or result.succeeded_count != record.expected_count
                ):
                    result_status = "untrusted"
                else:
                    result_status = "trusted"
                    succeeded_count = result.succeeded_count
                    artifact_id = result.artifact_id
                    collected_at = result.collected_at
                    items = [
                        BenchmarkSearchNoteRead(
                            note_id=item.note_id,
                            source_url=item.source_url,
                            title=item.title,
                            summary=item.summary,
                            user_id=item.user_id,
                        )
                        for item in result.items
                    ]

        return BenchmarkSearchRead(
            id=record.id,
            dossier_id=record.dossier_id,
            keyword_run_id=record.keyword_run_id,
            keyword_item_id=record.keyword_item_id,
            stage=record.stage,
            attempt=record.attempt,
            keyword=record.keyword,
            expected_count=record.expected_count,
            xhs_job_id=record.xhs_job_id,
            job_state=job.state.value,
            progress_current=job.progress_current,
            progress_total=job.progress_total,
            error_category=job.error_category,
            result_status=result_status,
            succeeded_count=succeeded_count,
            artifact_id=artifact_id,
            collected_at=collected_at,
            items=items,
            created_at=record.created_at,
        )

    def _require_trusted_probe(self, keyword_item_id: str) -> None:
        service = self._require_xhs()
        with self.database.session() as session:
            probes = session.scalars(
                select(BenchmarkSearchRecord)
                .where(
                    BenchmarkSearchRecord.keyword_item_id == keyword_item_id,
                    BenchmarkSearchRecord.stage == "probe",
                )
                .order_by(
                    BenchmarkSearchRecord.attempt.desc(),
                    BenchmarkSearchRecord.created_at.desc(),
                )
            ).all()
            for probe in probes:
                session.expunge(probe)
        for probe in probes:
            try:
                job = service.job_service.get(probe.xhs_job_id)
                if job.state is not JobState.succeeded:
                    continue
                result = service.get_search_results(probe.xhs_job_id)
            except (JobNotFound, CollectionFactNotFound):
                continue
            if (
                probe.expected_count == 2
                and result.expected_count == 2
                and result.succeeded_count == 2
                and result.keyword == probe.keyword
            ):
                return
        raise BenchmarkResearchConflict(
            "A trusted 2/2 benchmark probe is required before full collection."
        )

    def _current_keyword_item(
        self, dossier_id: str, keyword_item_id: str
    ) -> tuple[KeywordPlanRecord, KeywordPlanRunRecord]:
        with self.database.session() as session:
            if session.get(FinishedProductDossierRecord, dossier_id) is None:
                raise BenchmarkResearchNotFound(
                    f"Finished product dossier {dossier_id} does not exist."
                )
            latest_run = session.scalars(
                select(KeywordPlanRunRecord)
                .where(KeywordPlanRunRecord.dossier_id == dossier_id)
                .order_by(
                    KeywordPlanRunRecord.created_at.desc(),
                    KeywordPlanRunRecord.id.desc(),
                )
                .limit(1)
            ).first()
            if latest_run is None:
                raise BenchmarkResearchConflict(
                    "Generate or save a keyword plan before benchmark collection."
                )
            item = session.get(KeywordPlanRecord, keyword_item_id)
            if item is None or item.run_id != latest_run.id:
                raise BenchmarkResearchConflict(
                    "Benchmark collection may only use the current keyword-plan run."
                )
            session.expunge(item)
            session.expunge(latest_run)
            return item, latest_run

    def _latest_attempt(
        self, keyword_item_id: str, stage: str
    ) -> BenchmarkSearchRecord | None:
        with self.database.session() as session:
            record = session.scalars(
                select(BenchmarkSearchRecord)
                .where(
                    BenchmarkSearchRecord.keyword_item_id == keyword_item_id,
                    BenchmarkSearchRecord.stage == stage,
                )
                .order_by(
                    BenchmarkSearchRecord.attempt.desc(),
                    BenchmarkSearchRecord.created_at.desc(),
                )
                .limit(1)
            ).first()
            if record is not None:
                session.expunge(record)
            return record

    def _current_run_id(self, dossier_id: str) -> str | None:
        with self.database.session() as session:
            return session.scalar(
                select(KeywordPlanRunRecord.id)
                .where(KeywordPlanRunRecord.dossier_id == dossier_id)
                .order_by(
                    KeywordPlanRunRecord.created_at.desc(),
                    KeywordPlanRunRecord.id.desc(),
                )
                .limit(1)
            )

    def _require_dossier(self, dossier_id: str) -> None:
        with self.database.session() as session:
            if session.get(FinishedProductDossierRecord, dossier_id) is None:
                raise BenchmarkResearchNotFound(
                    f"Finished product dossier {dossier_id} does not exist."
                )

    def _require_xhs(self) -> XhsCollectionService:
        if self.xhs_service is None:
            raise BenchmarkResearchUnavailable(
                "XHS collection service is unavailable."
            )
        return self.xhs_service

    def _cancel_unbound_job(self, job_id: str) -> None:
        service = self._require_xhs()
        try:
            job = service.job_service.get(job_id)
            if job.state in {JobState.queued, JobState.running, JobState.needs_human}:
                service.job_service.transition(job_id, JobState.cancelled)
        except (JobNotFound, InvalidJobTransition):
            pass


def _append_unique(value: object, item: str) -> None:
    assert isinstance(value, list)
    if item not in value:
        value.append(item)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
