# Content Artifact Quarantine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Task 8 request-time physical deletion with durable, lease-driven quarantine and delayed garbage collection, while binding every package finalization to one exact builder.

**Architecture:** Business requests only persist failure state and enqueue cleanup facts. A dedicated `ArtifactCleanupService` claims durable rows, verifies Windows path and database ownership, atomically moves eligible files into a runtime-local quarantine directory, and deletes them only after a 24-hour grace period and a second verification. Package success and failure both use an exact build-token CAS.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2, Pydantic 2, SQLite, pytest, Windows filesystem semantics.

**Spec:** `docs/superpowers/specs/2026-08-18-content-artifact-quarantine-design.md`

## Global Constraints

- Runtime data remains under `D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench` and never enters Git.
- SQLite remains the only business database.
- API requests and startup recovery must never permanently delete Task 8 artifacts.
- Ambiguous path, link, junction, identity, ownership, or transaction outcome must retain the file and persist `needs_human`.
- Default quarantine grace period is exactly 24 hours.
- V1 exposes cleanup records as read-only; there is no force-delete endpoint.
- No automatic Xiaohongshu publishing is introduced.
- Existing unavailable Bailian and Android live checks remain `not_run`, never simulated as passing.

---

### Task 1: Durable cleanup records and exact builder identity

**Files:**
- Modify: `backend/app/features/content/models.py`
- Modify: `backend/app/features/content/schemas.py`
- Modify: `backend/app/db.py`
- Create: `backend/tests/content/test_artifact_cleanup_schema.py`

**Interfaces:**
- Consumes: existing `ContentPackageRecord`, `Database` schema migration and validation conventions.
- Produces: `ArtifactCleanupRecord`, `ArtifactCleanupRead`, `ContentPackageRecord.build_token`, migration marker `task8_artifact_quarantine_v1`.

- [ ] **Step 1: Write failing model and migration tests**

```python
def test_fresh_schema_has_cleanup_queue_and_build_token(database):
    inspection = inspect(database.engine)
    assert "artifact_gc_queue" in inspection.get_table_names()
    package_columns = {column["name"] for column in inspection.get_columns("content_packages")}
    assert "build_token" in package_columns


def test_cleanup_state_and_open_owner_are_physically_constrained(database):
    with pytest.raises(IntegrityError):
        insert_cleanup(database, state="unknown")
    first = insert_cleanup(database, owner_type="content_package", owner_id=PACKAGE_ID)
    with pytest.raises(IntegrityError):
        insert_cleanup(database, owner_type="content_package", owner_id=PACKAGE_ID)
    assert first.state == "pending"


def test_legacy_building_package_is_failed_and_enqueued_without_deleting_file(tmp_path):
    database, artifact = legacy_database_with_building_package(tmp_path)
    upgraded = Database(database.database_path, runtime_dir=tmp_path / "runtime")
    assert load_package(upgraded).status == "failed"
    assert load_cleanup(upgraded).state == "pending"
    assert artifact.exists()
```

- [ ] **Step 2: Run the schema tests and observe RED**

Run: `python -m pytest backend/tests/content/test_artifact_cleanup_schema.py -q`

Expected: FAIL because the queue table and `build_token` do not exist and startup does not enqueue recovery.

- [ ] **Step 3: Add the records, constraints, and strict read schema**

```python
class ArtifactCleanupRecord(Base):
    __tablename__ = "artifact_gc_queue"
    __table_args__ = (
        CheckConstraint(
            "owner_type IN ('material','content_package')",
            name="ck_artifact_gc_owner_type",
        ),
        CheckConstraint(
            "state IN ('pending','claimed','quarantined','deleted','needs_human','cancelled')",
            name="ck_artifact_gc_state",
        ),
        CheckConstraint("expected_size_bytes >= 0", name="ck_artifact_gc_size"),
        CheckConstraint(
            "length(expected_sha256) = 64 AND expected_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_artifact_gc_sha_format",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_artifact_gc_attempts"),
        Index(
            "uq_artifact_gc_open_owner",
            "owner_type", "owner_id", "relative_path",
            unique=True,
            sqlite_where=text("state IN ('pending','claimed','quarantined','needs_human')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    not_before: Mapped[datetime] = mapped_column(nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    quarantine_path: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_category: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column()
```

Add `build_token: Mapped[str | None] = mapped_column(String(36))` to `ContentPackageRecord`. Define `ArtifactCleanupRead` as a strict Pydantic model exposing the persisted fields but no mutation input schema.

- [ ] **Step 4: Add a retry-safe migration and physical schema validation**

In `Database`, add a transactionally marked `task8_artifact_quarantine_v1` migration. It must add `build_token`, create and validate the queue table, convert legacy `building` packages to `failed`, and insert cleanup rows without touching the filesystem. The marker is written only after all DDL/DML succeeds. Validation must check columns, CHECK definitions, partial unique index columns/predicate, and nullability.

- [ ] **Step 5: Run focused and migration regression tests**

Run: `python -m pytest backend/tests/content/test_artifact_cleanup_schema.py backend/tests/content/test_content_schema_migration.py -q`

Expected: PASS, including repeated startup and half-migration recovery.

- [ ] **Step 6: Commit Task 1**

```powershell
git add backend/app/features/content/models.py backend/app/features/content/schemas.py backend/app/db.py backend/tests/content/test_artifact_cleanup_schema.py
git commit -m "feat: persist artifact cleanup lifecycle"
```

---

### Task 2: Lease-driven quarantine service

**Files:**
- Create: `backend/app/features/content/cleanup.py`
- Modify: `backend/app/features/content/export.py`
- Create: `backend/tests/content/test_artifact_cleanup_service.py`

**Interfaces:**
- Consumes: `Database`, `ArtifactCleanupRecord`, trusted runtime directory, existing bounded file readers and Windows artifact identity helpers.
- Produces: `ArtifactCleanupCandidate(owner_type, owner_id, relative_path, expected_sha256, expected_size_bytes, reason, not_before)`, `ArtifactCleanupService.enqueue(candidate) -> ArtifactCleanupRead`, `claim_due(limit) -> list[str]`, `process_one(cleanup_id) -> ArtifactCleanupRead`, `recover_expired_leases() -> int`, `run_due_once(limit) -> int`, `list_records() -> list[ArtifactCleanupRead]`, `get_record(cleanup_id) -> ArtifactCleanupRead | None`.

- [ ] **Step 1: Write failing lifecycle and concurrency tests**

```python
def test_enqueue_is_idempotent_for_open_owner(cleanup_service):
    first = cleanup_service.enqueue(candidate())
    second = cleanup_service.enqueue(candidate())
    assert second.id == first.id


def test_two_workers_only_one_claims_due_record(cleanup_service, second_service):
    record = cleanup_service.enqueue(candidate(not_before=past()))
    claimed = run_concurrently(
        lambda: cleanup_service.claim_due(limit=1),
        lambda: second_service.claim_due(limit=1),
    )
    assert sum(record.id in ids for ids in claimed) == 1


def test_ambiguous_link_is_needs_human_and_retained(cleanup_service, linked_candidate):
    result = cleanup_service.process_one(linked_candidate.id)
    assert result.state == "needs_human"
    assert linked_candidate.file.exists()


def test_quarantine_then_delete_only_after_24_hours(cleanup_service, clock):
    record = cleanup_service.enqueue(candidate(not_before=clock.now))
    quarantined = cleanup_service.process_one(record.id)
    assert quarantined.state == "quarantined"
    assert quarantined.quarantine_path.startswith("artifacts-quarantine/")
    assert cleanup_service.process_one(record.id).state == "quarantined"
    clock.advance(timedelta(hours=24))
    assert cleanup_service.process_one(record.id).state == "deleted"
```

- [ ] **Step 2: Run the cleanup service tests and observe RED**

Run: `python -m pytest backend/tests/content/test_artifact_cleanup_service.py -q`

Expected: FAIL because the service and lifecycle do not exist.

- [ ] **Step 3: Implement durable enqueue and lease claims**

```python
class ArtifactCleanupService:
    def __init__(self, database: Database, *, runtime_dir: Path, clock: Callable[[], datetime] = utc_now):
        self.database = database
        self.runtime_dir = runtime_dir.resolve(strict=True)
        self.clock = clock

    def claim_due(self, *, limit: int = 10) -> list[str]:
        token = str(uuid4())
        now = self.clock()
        expires = now + timedelta(minutes=5)
```

Define `ArtifactCleanupCandidate` as a frozen dataclass containing the seven fields named in the interface. Complete `claim_due()` by selecting at most `limit` due IDs ordered by `created_at`, conditionally updating each row from `pending` to `claimed` with `lease_token=token` and `lease_expires_at=expires`, and returning only IDs whose UPDATE rowcount is one. `enqueue()` returns the existing open row on uniqueness conflict. `recover_expired_leases()` returns expired `claimed` rows to `pending` and increments attempts.

- [ ] **Step 4: Implement fail-closed ownership proof and quarantine rename**

Add a single helper in `export.py` that returns `trusted`, `ambiguous`, or `missing` rather than treating `None` as “no conflict.” `process_one()` must keep the database lease while proving ownership. Hashing, large reads, rename, and waits must remain outside SQLite write transactions. The sole bounded exception is final deletion: after opening and verifying the identity-bound delete handle, a short `BEGIN IMMEDIATE` transaction revalidates the lease and complete reference snapshot, performs only the handle-bound disposition, writes the `deleted` CAS, and commits immediately so a new reference writer cannot pass between authorization and deletion. It rechecks the lease and all reference facts immediately before and after an atomic same-volume rename. Any changed fact moves the row to `needs_human`; it never guesses and never follows a link or junction.

- [ ] **Step 5: Implement delayed final deletion**

For `quarantined` rows, require `not_before = quarantined_at + timedelta(hours=24)`. Recompute path containment, physical identity, size, SHA, owner references and lease before calling the existing handle-bound removal primitive. Arm the Windows handle disposition before the database CAS, but keep the handle open: any UPDATE/commit failure must disarm it before close; if disarm fails, retain the `BEGIN IMMEDIATE` writer boundary and persist `needs_human/delete_outcome_ambiguous` before releasing the handle. Mark `deleted` only after the exact database commit. A missing quarantined file is `deleted` with `last_error_category="already_missing"` only when no live reference exists, using a fresh clock and a strictly unexpired lease CAS. Install physically validated material/package INSERT and path-UPDATE triggers so no Windows-equivalent path can reference any claimed/quarantined/deleted/needs-human quarantine path; historical conflicts fail migration closed without deleting bytes.

- [ ] **Step 6: Run lifecycle, path, and fault-injection tests**

Run: `python -m pytest backend/tests/content/test_artifact_cleanup_service.py backend/tests/content/test_round3_hardening.py backend/tests/content/test_round5_hardening.py -q`

Expected: PASS; concurrent claims, link/junction ambiguity, rename/commit interruption and restart recovery retain or quarantine safely.

- [ ] **Step 7: Commit Task 2**

```powershell
git add backend/app/features/content/cleanup.py backend/app/features/content/export.py backend/tests/content/test_artifact_cleanup_service.py
git commit -m "feat: quarantine unreferenced content artifacts"
```

---

### Task 3: Replace request-time deletion and bind package finalization

**Files:**
- Modify: `backend/app/features/content/service.py`
- Modify: `backend/app/db.py`
- Modify: `backend/tests/content/test_round5_hardening.py`
- Create: `backend/tests/content/test_quarantine_integration.py`

**Interfaces:**
- Consumes: `ArtifactCleanupService.enqueue()`, `ContentPackageRecord.build_token`.
- Produces: request paths with no physical delete; exact success/failure CAS over package, item, revision, path and build token.

- [ ] **Step 1: Write failing integration tests for the two final-review blockers**

```python
def test_material_database_failure_enqueues_without_deleting(service, injected_commit_failure):
    artifact = service.write_material_then_fail(injected_commit_failure)
    assert artifact.exists()
    assert cleanup_for_path(artifact).state == "pending"


def test_failed_finalizer_cannot_touch_taken_over_reservation(service):
    reservation = reserve_package(service)
    replace_builder_token(reservation.id)
    assert service.fail_reservation(reservation) is False
    assert load_package(reservation.id).status == "building"


def test_reference_created_while_cleanup_pending_is_retained(service):
    cleanup = enqueue_package_cleanup(service)
    create_material_reference_to_same_file(cleanup.relative_path)
    process_cleanup(cleanup.id)
    assert load_cleanup(cleanup.id).state == "needs_human"
    assert runtime_file(cleanup.relative_path).exists()
```

- [ ] **Step 2: Run the integration tests and observe RED**

Run: `python -m pytest backend/tests/content/test_quarantine_integration.py -q`

Expected: FAIL because request paths still call physical deletion and failed CAS omits exact builder fields.

- [ ] **Step 3: Inject the cleanup service and remove direct deletion calls**

Change `ContentService.__init__` to receive `cleanup_service: ArtifactCleanupService`. Replace `_cleanup_unpersisted_material()` and `_cleanup_unreferenced_artifact()` with enqueue-only methods. Remove every request-time call to `remove_contained_regular()` from `service.py`. Database startup recovery similarly creates cleanup rows and never imports or calls the delete helper.

- [ ] **Step 4: Bind success and failure to the exact reservation**

Generate `build_token = str(uuid4())` at reservation time. Both ready and failed updates must use this predicate:

```python
ContentPackageRecord.id == package_id,
ContentPackageRecord.content_item_id == item_id,
ContentPackageRecord.revision_id == expected_revision_id,
ContentPackageRecord.status == "building",
ContentPackageRecord.path == package_path,
ContentPackageRecord.build_token == build_token,
```

The ready package update and item `exported` update remain in one transaction. On CAS loss, enqueue the produced path using the losing builder's recorded identity; do not alter the winner's package row.

- [ ] **Step 5: Make startup recovery enqueue-only**

For each legacy or interrupted `building` package, atomically set `failed`, preserve its recorded path and identity, and insert an idempotent cleanup row. Invalid owner/path facts become `needs_human`. No startup code opens or deletes the artifact.

- [ ] **Step 6: Run Task 8 regression tests**

Run: `python -m pytest backend/tests/content -q`

Expected: PASS with the original review, image, ZIP, migration, round 1—5 and new quarantine tests.

- [ ] **Step 7: Commit Task 3**

```powershell
git add backend/app/features/content/service.py backend/app/db.py backend/tests/content/test_round5_hardening.py backend/tests/content/test_quarantine_integration.py
git commit -m "fix: defer content artifact deletion"
```

---

### Task 4: Worker lifecycle and read-only cleanup API

**Files:**
- Modify: `backend/app/settings.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/features/content/cleanup.py`
- Modify: `backend/app/features/content/api.py`
- Modify: `backend/app/features/content/service.py`
- Create: `backend/tests/content/test_cleanup_api.py`
- Create: `backend/tests/content/test_cleanup_worker.py`

**Interfaces:**
- Consumes: `ArtifactCleanupService.run_due_once()`, `ArtifactCleanupRead`.
- Produces: bounded persistent worker lifecycle and `GET /api/v1/artifact-cleanups` read APIs.

- [ ] **Step 1: Write failing API and worker recovery tests**

```python
def test_cleanup_api_is_read_only(client, cleanup_record):
    response = client.get("/api/v1/artifact-cleanups")
    assert response.status_code == 200
    assert response.json()[0]["state"] == cleanup_record.state
    assert client.delete(f"/api/v1/artifact-cleanups/{cleanup_record.id}").status_code == 405


def test_worker_shutdown_is_bounded(app_with_blocked_cleanup):
    started = monotonic()
    app_with_blocked_cleanup.close()
    assert monotonic() - started < 1.0


def test_expired_claim_recovers_after_restart(app_factory, expired_claim):
    app = app_factory()
    assert get_cleanup(app, expired_claim.id).state == "pending"
```

- [ ] **Step 2: Run API and worker tests and observe RED**

Run: `python -m pytest backend/tests/content/test_cleanup_api.py backend/tests/content/test_cleanup_worker.py -q`

Expected: FAIL because the service is not wired to the app and no read routes or worker exist.

- [ ] **Step 3: Add bounded worker settings and lifecycle**

Add settings with exact defaults and bounds:

```python
artifact_cleanup_poll_seconds: float = Field(default=30.0, ge=1.0, le=3600.0)
artifact_cleanup_batch_size: int = Field(default=10, ge=1, le=100)
artifact_cleanup_grace_hours: int = Field(default=24, ge=1, le=168)
```

Create one worker owned by the app lifespan. It uses a stop event, persists every state before waiting, processes at most one bounded batch per poll, and is stopped before the database closes. Shutdown must not wait on an unbounded sleep; the stop event wakes the poll immediately.

- [ ] **Step 4: Wire services and add read routes**

Construct one `ArtifactCleanupService` after `Database`, inject it into `ContentService`, recover expired leases, then start the worker. Add:

```python
@router.get("/artifact-cleanups", response_model=list[ArtifactCleanupRead])
def list_artifact_cleanups(request: Request) -> list[ArtifactCleanupRead]:
    service: ArtifactCleanupService | None = request.app.state.artifact_cleanup_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service.list_records()

@router.get("/artifact-cleanups/{cleanup_id}", response_model=ArtifactCleanupRead)
def get_artifact_cleanup(cleanup_id: str, request: Request) -> ArtifactCleanupRead:
    service: ArtifactCleanupService | None = request.app.state.artifact_cleanup_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    record = service.get_record(cleanup_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Artifact cleanup record does not exist.")
    return record
```

No POST, PATCH or DELETE route is added.

- [ ] **Step 5: Run lifecycle and API regression tests**

Run: `python -m pytest backend/tests/content/test_cleanup_api.py backend/tests/content/test_cleanup_worker.py backend/tests/test_health.py -q`

Expected: PASS; unavailable database returns truthful 503 and shutdown remains bounded.

- [ ] **Step 6: Commit Task 4**

```powershell
git add backend/app/settings.py backend/app/main.py backend/app/features/content/cleanup.py backend/app/features/content/api.py backend/app/features/content/service.py backend/tests/content/test_cleanup_api.py backend/tests/content/test_cleanup_worker.py
git commit -m "feat: run durable artifact cleanup worker"
```

---

### Task 5: Close approved Task 8 minors and verify the redesigned boundary

**Files:**
- Modify: `backend/app/features/content/export.py`
- Modify: `backend/app/features/content/api.py`
- Modify: `backend/app/features/content/service.py`
- Modify: `backend/tests/content/test_export.py`
- Modify: `backend/tests/content/test_workflow.py`
- Modify: `.superpowers/sdd/2026-08-17-xhs-workbench-implementation/progress.md`
- Modify: `.superpowers/sdd/2026-08-17-xhs-workbench-implementation/task-8-report.md`

**Interfaces:**
- Consumes: completed quarantine lifecycle and existing content workflow.
- Produces: closed Task 8 minor ledger, full verification evidence and review-ready diff.

- [ ] **Step 1: Write failing tests for the two approved Task 8 minors**

```python
def test_archive_byte_limit_includes_zip_container_overhead():
    with pytest.raises(UnsafeContentPath):
        deterministic_zip(entries_near_archive_limit(), manifest={})


def test_missing_product_precedes_model_configuration_error(client_without_model):
    response = client_without_model.post("/api/v1/content-items", json=valid_missing_product_payload())
    assert response.status_code == 404
```

- [ ] **Step 2: Run the minor tests and observe RED**

Run: `python -m pytest backend/tests/content/test_export.py backend/tests/content/test_workflow.py -q`

Expected: FAIL at the archive-overhead boundary and with create returning 503 before product validation.

- [ ] **Step 3: Enforce final archive bytes and service-layer validation order**

After building the ZIP bytes, reject `len(archive) > MAX_PACKAGE_BYTES`; keep the existing uncompressed aggregate cap as a separate constant. Remove the API-level model precheck from `create_content_item`; inside the service, validate product, opportunity, evidence and materials first, then return `ContentModelUnavailable` before making a model call.

- [ ] **Step 4: Run all automated verification**

Run:

```powershell
python -m pytest backend/tests/content -q
python -m pytest backend/tests -q
python -m compileall -q backend/app backend/tests
git diff --check
```

Expected: all content and backend tests pass; the opt-in live Bailian test may remain one explicit skip when no key is configured; compile and diff checks exit 0.

- [ ] **Step 5: Run an independent requirements and code review**

The reviewer must probe lease takeover, reference creation during cleanup, ambiguous links/junctions, move/commit interruption, restart, exact builder CAS, no request/startup permanent deletion, API read-only behavior, and both closed minors. Task 8 is accepted only with no Critical or Important findings.

- [ ] **Step 6: Update the ledger and report with truthful live status**

Record exact test counts, commits, review result, and these unchanged live facts: Bailian `not_run` without API key, Android `not_run` without device, seven-day UAT `not_run`. Mark Task 8 complete only after the independent review is clean.

- [ ] **Step 7: Commit Task 5**

```powershell
git add backend/app/features/content/export.py backend/app/features/content/api.py backend/app/features/content/service.py backend/tests/content/test_export.py backend/tests/content/test_workflow.py
git commit -m "test: verify quarantined content cleanup"
```
