# Xiaohongshu Account and Note Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production read-only `xhs-cli` path that collects account profiles and notes, persists trusted evidence, exposes real task/API/UI state, and supplies account-note evidence to analysis.

**Architecture:** A provider-neutral adapter runs a strict subprocess allowlist and returns existing `CollectionResult` facts. A durable collection service owns reserved jobs, bounded account-sample artifacts, keyword-search artifacts, exact shop N/N and shutdown; account/note tables and analysis trust checks never depend on native CLI JSON. The frontend starts collections and displays only persisted API state.

**2026-08-20 scope correction:** Account homepage collection is a latest-10 sample (default and hard limit 10), not full-history N/N. Fewer than 10 visible notes succeeds as `sample_exhausted`; overflow is truncated before artifact/SQLite persistence. Keyword research is separate: the tutorial calls for 5–10 qualifying notes per actual keyword, with a 2-note first-pass completeness check. Historical 62-note evidence remains preserved and is not the default input for new analysis. Any older `expected_note_count` or account-note exact-N/N examples below are superseded by this correction; strict full N/N remains unchanged for bounded shop products/images/manifests.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, SQLite, Pydantic 2, `subprocess`, React 19, TypeScript, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-08-18-xhs-account-note-collection-design.md`

## Global Constraints

- V1 registers only `xhs-cli` for `search_notes` and `fetch_account`; Android remains the product collector.
- Only `status`, `whoami`, `search`, `read`, `user`, and `user-posts` may execute; never invoke a Shell.
- API input cannot choose executable, command, URL, Cookie, token, environment, or evidence path.
- Cookie/token values never enter logs, database facts, artifacts, or API responses.
- Only a trusted `bounded_sample` or truthful `sample_exhausted` result may persist normalized account-note facts and succeed atomically; captcha/limit/unknown ownership remains non-success.
- Login expiry, captcha, rate-limit and account visibility failures are truthful `needs_human` facts.
- No platform write operation and no automatic publication.

---

### Task 1: Strict read-only CLI adapter

**Files:**
- Create: `backend/app/adapters/xhs_cli_read.py`
- Modify: `backend/app/settings.py`
- Modify: `backend/app/adapters/contracts.py`
- Test: `backend/tests/xhs/test_cli_adapter.py`
- Test: `backend/tests/test_settings.py`

**Interfaces:**
- Consumes: `CollectionRequest`, `CollectionItem`, `CollectionResult`.
- Produces: `XhsCliReadAdapter.search_notes(request) -> CollectionResult` and `fetch_account(request) -> CollectionResult`.

- [ ] **Step 1: Write failing command-boundary tests**

```python
def test_search_uses_argument_array_and_json_allowlist(fake_runner):
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)
    adapter.search_notes(CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "收纳", "job_id": JOB_ID},
        expected_count=2,
    ))
    assert fake_runner.argv == ["xhs", "search", "收纳", "--json"]
    assert fake_runner.shell is False

@pytest.mark.parametrize("field", ["command", "executable", "cookie", "url", "env"])
def test_untrusted_execution_fields_are_rejected(field):
    with pytest.raises(ValueError):
        XhsCliSearchRequest.model_validate({"keyword": "收纳", field: "bad"})
```

- [ ] **Step 2: Run the adapter tests and observe RED**

Run: `python -m pytest backend/tests/xhs/test_cli_adapter.py backend/tests/test_settings.py -q`

Expected: collection error because `xhs_cli_read` and trusted settings do not exist.

- [ ] **Step 3: Implement strict schemas, runner and normalization**

```python
class XhsCliReadAdapter:
    capabilities = frozenset({"search_notes", "fetch_account"})

    def search_notes(self, request: CollectionRequest) -> CollectionResult:
        return self._collect(["search", request.parameters["keyword"]], request)

    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        profile = self._invoke(["user", request.parameters["user_id"]])
        notes = self._invoke(["user-posts", request.parameters["user_id"]])
        return self._normalize_account(profile=profile, notes=notes, request=request)

def run_xhs_json(argv: Sequence[str], *, timeout_seconds: float,
                 max_stdout_bytes: int = 5 * 1024 * 1024) -> dict[str, Any]:
    completed = subprocess.run(list(argv), shell=False, capture_output=True,
                               timeout=timeout_seconds, check=False)
    return decode_bounded_json(completed, max_stdout_bytes=max_stdout_bytes)
```

Implement argument arrays, timeout, stdout/stderr caps, strict UTF-8/JSON, stable note/account IDs, canonical source URLs, duplicate accounting, safe error categories and recursive credential-key redaction. Add `xhs_cli_executable` and bounded `xhs_cli_timeout_seconds` to Settings.

- [ ] **Step 4: Prove login/captcha/rate-limit and malformed output states**

Run: `python -m pytest backend/tests/xhs/test_cli_adapter.py -q`

Expected: all tests pass; no error response contains fake Cookie/token sentinel values.

- [ ] **Step 5: Commit Task 1**

```powershell
git add backend/app/adapters backend/app/settings.py backend/tests/xhs backend/tests/test_settings.py
git commit -m "feat: add read-only xhs cli adapter"
```

### Task 2: Durable account, note and evidence schema

**Files:**
- Create: `backend/app/features/xhs/models.py`
- Create: `backend/app/features/xhs/schemas.py`
- Modify: `backend/app/db.py`
- Test: `backend/tests/xhs/test_schema_migration.py`
- Test: `backend/tests/xhs/test_evidence_persistence.py`

**Interfaces:**
- Consumes: exact normalized `CollectionResult`.
- Produces: `XhsAccountProfileRecord`, `XhsAccountNoteRecord`, strict read schemas and migration marker `xhs_account_note_evidence_v1`.

- [ ] **Step 1: Write failing fresh/legacy/tamper schema tests**

```python
def test_note_identity_and_account_ownership_are_physical_constraints(database):
    assert unique_columns(database, "xhs_account_notes") == {("note_id", "user_id")}
    assert foreign_key(database, "xhs_account_notes", "user_id") == (
        "xhs_account_profiles", "user_id", "CASCADE"
    )

def test_marker_present_with_missing_constraint_fails_closed(populated_database):
    weaken_note_unique_constraint(populated_database)
    with pytest.raises(SchemaMigrationError):
        Database(populated_database.path)
```

- [ ] **Step 2: Run schema tests and observe RED**

Run: `python -m pytest backend/tests/xhs/test_schema_migration.py -q`

Expected: missing tables and marker validation.

- [ ] **Step 3: Implement tables and retry-safe validation-only marker behavior**

Required columns include source URL, collection job/artifact identity, raw digest and collected time. Validate CHECK/UNIQUE/FK/index definitions and existing rows before writing the marker; marker-present startup is validation-only.

- [ ] **Step 4: Add exact persistence transaction tests**

```python
def test_complete_result_persists_facts_and_job_success_in_one_transaction(service, complete_result):
    job = service.finalize_account_result(complete_result)
    assert job.state is JobState.succeeded
    assert service.list_notes(complete_result.items[0].data["user_id"])

def test_partial_or_rejected_result_persists_no_normalized_note(service, partial_result):
    job = service.finalize_account_result(partial_result)
    assert job.state is JobState.needs_human
    assert service.list_notes(partial_result.account_user_id) == []

def test_cancel_race_leaves_no_note_with_cancelled_job(service, complete_result):
    service.jobs.transition(complete_result.job_id, JobState.cancelled)
    with pytest.raises(CollectionStateError):
        service.finalize_account_result(complete_result)
    assert service.list_notes(complete_result.account_user_id) == []
```

Run: `python -m pytest backend/tests/xhs/test_schema_migration.py backend/tests/xhs/test_evidence_persistence.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add backend/app/features/xhs backend/app/db.py backend/tests/xhs
git commit -m "feat: persist trusted account note evidence"
```

### Task 3: Collection service, reserved jobs and API

**Files:**
- Create: `backend/app/features/xhs/service.py`
- Create: `backend/app/features/xhs/api.py`
- Modify: `backend/app/adapters/registry.py`
- Modify: `backend/app/api/jobs.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/xhs/test_collection_service.py`
- Test: `backend/tests/xhs/test_collection_api.py`
- Test: `backend/tests/test_jobs_hardening.py`

**Interfaces:**
- Consumes: `XhsCliReadAdapter`, `JobService`, runtime directory and database.
- Produces: `XhsCollectionService.submit_account`, `submit_search`, list/read methods and HTTP 202 routes.

- [ ] **Step 1: Write failing admission, reservation and lifecycle tests**

```python
def test_account_collection_returns_202_and_durable_queued_job(client):
    response = client.post("/api/v1/accounts/u1/collections", json={"sample_limit": 10})
    assert response.status_code == 202
    assert client.get(f"/api/v1/jobs/{response.json()['job_id']}").json()["state"] == "queued"

def test_generic_job_api_cannot_claim_or_attach_xhs_artifacts(client, xhs_job_id):
    assert client.post(f"/api/v1/jobs/{xhs_job_id}/claim").status_code == 422
    assert client.post(f"/api/v1/jobs/{xhs_job_id}/artifacts", json={"kind": "xhs_raw"}).status_code == 422

def test_shutdown_rejects_new_collection_and_no_late_success(service):
    assert service.close() in {True, False}
    with pytest.raises(CollectionServiceClosed):
        service.submit_account("u1", 2)

def test_external_command_failure_has_persisted_sanitized_fact(service, failing_adapter):
    job = service.run_once(failing_adapter)
    assert job.state is JobState.failed
    assert "secret-sentinel" not in json.dumps(job, default=str)
```

- [ ] **Step 2: Run tests and observe RED**

Run: `python -m pytest backend/tests/xhs/test_collection_service.py backend/tests/xhs/test_collection_api.py -q`

Expected: missing service and routes.

- [ ] **Step 3: Implement one bounded daemon worker and atomic finalizer**

```python
class XhsCollectionService:
    def submit_account(self, user_id: str, sample_limit: int = 10) -> Job:
        return self._submit("xhs_account_collection", user_id, min(sample_limit, 10))
    def submit_search(self, keyword: str, expected_count: int) -> Job:
        return self._submit("xhs_note_search", keyword, expected_count)
    def close(self) -> bool:
        self._closed.set()
        self._worker.join(timeout=0.25)
        return not self._worker.is_alive()
```

Use reserved types `xhs_account_collection` and `xhs_note_search`, one admitted task at a time, cancellation checkpoints before external invocation and before DB finalization, raw artifact write with source/hash/counts and an atomic fact+job success transaction.

- [ ] **Step 4: Wire registry, application lifecycle and read APIs**

Implement the exact API paths from the spec and return 503 for unavailable database/adapter, 404 for missing facts, 409 for state conflicts and 422 for invalid input.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest backend/tests/xhs backend/tests/test_jobs_hardening.py backend/tests/test_jobs_api.py -q`

Run: `python -m pytest backend/tests -q`

Expected: focused and full backend pass; opt-in live tests may skip only for absent local login.

- [ ] **Step 6: Commit Task 3**

```powershell
git add backend/app backend/tests
git commit -m "feat: run durable account note collections"
```

### Task 4: Analysis trust and account-note grounding

**Files:**
- Modify: `backend/app/features/analysis/service.py`
- Modify: `backend/app/features/analysis/schemas.py`
- Test: `backend/tests/analysis/test_account_note_grounding.py`
- Test: `backend/tests/analysis/test_evidence_grounding.py`

**Interfaces:**
- Consumes: persisted account/note records and their trusted JobArtifact producer.
- Produces: `account-note:<id>` evidence discovery and strict grounding facts.

- [ ] **Step 1: Write failing ownership and tamper tests**

```python
def test_discovery_returns_only_trusted_notes_for_requested_account(analysis_service, note_for_u1, note_for_u2):
    ids = {row.evidence_id for row in analysis_service.list_evidence(account_user_id="u1")}
    assert ids == {f"account-note:{note_for_u1.id}"}

def test_cross_account_note_reference_is_rejected_before_model_call(analysis_service, note_for_u2, model_spy):
    with pytest.raises(EvidenceAccountMismatch):
        analysis_service.create(account_payload("u1", f"account-note:{note_for_u2.id}"))
    assert model_spy.calls == 0

def test_tampered_raw_artifact_makes_note_ineligible(analysis_service, trusted_note):
    trusted_note.artifact_path.write_bytes(b"tampered")
    assert analysis_service.list_evidence(account_user_id=trusted_note.user_id)[0].eligible_for_opportunity is False

def test_claims_can_cite_account_note_ids(analysis_service, trusted_note, grounded_model):
    result = analysis_service.create(account_payload(trusted_note.user_id, f"account-note:{trusted_note.id}"))
    assert result.output["claims"][0]["evidence_ids"] == [f"account-note:{trusted_note.id}"]
```

- [ ] **Step 2: Run tests and observe RED**

Run: `python -m pytest backend/tests/analysis/test_account_note_grounding.py -q`

Expected: unknown evidence prefix or missing discovery rows.

- [ ] **Step 3: Implement trusted note verification**

Verify reserved job type/state, producer, exact artifact path, size/hash, file identity, metadata/file equality, user ID, note ID and persisted raw digest. Add normalized public note facts to the model evidence payload; never send raw credentials or unrelated accounts.

- [ ] **Step 4: Run analysis and full backend tests**

Run: `python -m pytest backend/tests/analysis backend/tests/xhs -q`

Run: `python -m pytest backend/tests -q`

Expected: all pass except the existing opt-in live gate.

- [ ] **Step 5: Commit Task 4**

```powershell
git add backend/app/features/analysis backend/tests/analysis
git commit -m "feat: ground analysis in account note evidence"
```

### Task 5: Operator UI, controlled E2E and live gate

**Files:**
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/pages/AccountPage.tsx`
- Modify: `frontend/src/pages/RadarPage.tsx`
- Modify: `frontend/src/pages/AccountPage.test.tsx`
- Modify: `frontend/src/pages/RadarPage.test.tsx`
- Modify: `frontend/e2e/fixture_app.py`
- Modify: `frontend/e2e/empty-to-package.spec.ts`
- Modify: `docs/UAT_CHECKLIST.md`
- Modify: `docs/IMPLEMENTATION_STATUS.md`

**Interfaces:**
- Consumes: collection and read APIs from Task 3 and analysis evidence from Task 4.
- Produces: truthful operator workflow and controlled E2E account-note evidence.

- [ ] **Step 1: Write failing UI state and single-flight tests**

```tsx
it("shows needs-human detail and preserves the old job before retry", async () => { /* exact API fixture */ })
it("does not submit a second collection while the first request is pending", async () => { /* deferred promise */ })
it("renders note source links and evidence ids", async () => { /* trusted rows */ })
```

- [ ] **Step 2: Implement account collection and keyword search UI**

Poll only returned job IDs with a bounded interval, stop on terminal state/unmount, surface stale-state errors, and never infer an N total. Clearly label local login as an operator prerequisite.

- [ ] **Step 3: Replace the E2E shortcut with generated account-note fixtures**

The controlled adapter must return normalized account/profile/note JSON through the same job/service path. The E2E must select those `account-note:*` evidence IDs for analysis before reaching product/content/ZIP.

- [ ] **Step 4: Run frontend and controlled end-to-end verification**

Run: `npm test --prefix frontend`

Run: `npm run build --prefix frontend`

Run: `npm run test:e2e --prefix frontend`

Run: `npm run test:e2e --prefix frontend -- --repeat-each=5`

Expected: all pass; no browser console/page errors; no pre-seeded account-note database rows.

- [ ] **Step 5: Add guarded live smoke and documentation**

Add an opt-in test requiring an explicit environment flag and local `xhs-cli status`. Without a valid local session it prints `not_run`, creates no successful facts and does not modify login state.

- [ ] **Step 6: Run repository verification and commit**

Run: `powershell -ExecutionPolicy Bypass -File scripts/verify.ps1`

```powershell
git add frontend docs backend/tests/integration .superpowers/sdd/2026-08-17-xhs-workbench-implementation/progress.md
git commit -m "feat: complete account note evidence workflow"
```
