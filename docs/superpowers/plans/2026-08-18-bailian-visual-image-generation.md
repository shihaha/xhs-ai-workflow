# Bailian Vision and Image Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add provider-neutral vision analysis and Bailian-compatible image generation that creates managed output images, records durable model facts and retains mandatory human visual approval before export.

**Architecture:** Separate text, vision and image-generation adapter contracts prevent provider-specific payloads from entering content logic. Durable media runs and reserved jobs call the adapters, validate all input/output bytes, and persist generated images through the existing material/cleanup outbox. Vision output is advisory and cannot approve a revision.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, SQLite, Pydantic 2, httpx, Pillow, React 19, TypeScript, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-08-18-bailian-visual-image-generation-design.md`

## Global Constraints

- Text, vision and image models/configuration are separate; requests cannot override endpoint/model/path.
- All provider responses use bounded retries, sanitized attempt facts and strict Pydantic output validation.
- Vision reads only current managed materials after path/hash/size/MIME/identity verification.
- Generated images must be fully decoded and pass byte, pixel and format limits before becoming materials.
- File creation uses the existing cleanup outbox; request/startup paths never permanently delete artifacts.
- Vision output cannot write an approve review or bypass per-image human visual checks.
- No automatic publication and no placeholder image may count as a generated result.

---

### Task 1: Media adapter contracts and Bailian clients

**Files:**
- Modify: `backend/app/adapters/contracts.py`
- Create: `backend/app/adapters/bailian_media.py`
- Modify: `backend/app/settings.py`
- Test: `backend/tests/media/test_adapter_contracts.py`
- Test: `backend/tests/media/test_bailian_media.py`
- Test: `backend/tests/test_settings.py`

**Interfaces:**
- Produces: `VisionRequest`, `VisionResult`, `ImageGenerationRequest`, `GeneratedImage`, `VisionAdapter`, `ImageGenerationAdapter`, `BailianVisionAdapter`, `BailianImageGenerationAdapter`.

- [ ] **Step 1: Write failing strict contract tests**

```python
class VisualAssessment(BaseModel):
    summary: str
    plan_match: bool
    text_readability: str
    defects: list[str]
    safety_issues: list[str]
    suggestions: list[str]

def test_request_forbids_endpoint_model_and_absolute_path():
    with pytest.raises(ValidationError):
        ImageGenerationRequest.model_validate({"prompt": "图", "model": "arbitrary", "path": "C:/out.png"})

def test_provider_result_rejects_unbounded_usage_and_unknown_fields():
    with pytest.raises(ValidationError):
        VisionResult.model_validate({"output": {}, "usage": {"tokens": 1_000_000_001}, "extra": True})
```

- [ ] **Step 2: Run tests and observe RED**

Run: `python -m pytest backend/tests/media/test_adapter_contracts.py backend/tests/media/test_bailian_media.py -q`

Expected: missing media contracts and adapters.

- [ ] **Step 3: Implement separate configured providers**

```python
class VisionAdapter(Protocol):
    configured: bool
    provider: str
    model: str
    def analyze_images(self, request: VisionRequest,
                       schema: type[BaseModel]) -> VisionResult:
        raise NotImplementedError

class ImageGenerationAdapter(Protocol):
    configured: bool
    provider: str
    model: str
    def generate_images(self, request: ImageGenerationRequest) -> list[GeneratedImage]:
        raise NotImplementedError
```

Use trusted Settings for vision/image model names, base URL, timeout, attempts and API key. Sanitize provider attempts using shared safe categories; never persist body/header/API key. Handle asynchronous provider task polling with a bounded deadline and terminal provider status allowlist.

- [ ] **Step 4: Verify auth, 429, 5xx, timeout and schema failures**

Run: `python -m pytest backend/tests/media backend/tests/test_settings.py -q`

Expected: all pass; credential sentinels absent from exceptions/results.

- [ ] **Step 5: Commit Task 1**

```powershell
git add backend/app/adapters backend/app/settings.py backend/tests/media backend/tests/test_settings.py
git commit -m "feat: add Bailian media adapter contracts"
```

### Task 2: Durable media runs and migration

**Files:**
- Create: `backend/app/features/media/models.py`
- Create: `backend/app/features/media/schemas.py`
- Modify: `backend/app/db.py`
- Test: `backend/tests/media/test_media_schema.py`
- Test: `backend/tests/media/test_media_migration.py`

**Interfaces:**
- Produces: `ContentMediaRunRecord`, strict create/read schemas and migration marker `content_media_runs_v1`.

- [ ] **Step 1: Write failing schema, uniqueness and marker tests**

```python
def test_one_open_generation_per_revision_plan_entry(database, open_run):
    with pytest.raises(IntegrityError):
        insert_duplicate_open_run(database, open_run.content_item_id, open_run.revision_id, open_run.plan_entry_id)

def test_run_status_provider_model_and_output_identity_checks(database):
    with pytest.raises(IntegrityError):
        insert_media_run(database, status="succeeded", output_material_id=None)

def test_marker_present_with_weakened_constraint_fails_closed(populated_database):
    weaken_open_run_index(populated_database)
    with pytest.raises(SchemaMigrationError):
        Database(populated_database.path)
```

- [ ] **Step 2: Run tests and observe RED**

Run: `python -m pytest backend/tests/media/test_media_schema.py backend/tests/media/test_media_migration.py -q`

Expected: missing table and migration marker.

- [ ] **Step 3: Implement physical constraints and validation-only marker startup**

Persist capability, item/revision/plan-entry identity, job, status, provider/model/prompt version, allowed evidence/material IDs, output material/artifact, usage/duration/attempts/error and timestamps. Add a partial unique index for open generation runs.

- [ ] **Step 4: Run fresh, half-migration, populated legacy and tamper tests**

Run: `python -m pytest backend/tests/media/test_media_schema.py backend/tests/media/test_media_migration.py -q`

Expected: all pass; marker-present missing/weak schema fails without repair.

- [ ] **Step 5: Commit Task 2**

```powershell
git add backend/app/features/media backend/app/db.py backend/tests/media
git commit -m "feat: persist content media runs"
```

### Task 3: Generated-image material persistence and visual trust

**Files:**
- Create: `backend/app/features/media/service.py`
- Modify: `backend/app/features/content/service.py`
- Modify: `backend/app/features/content/schemas.py`
- Test: `backend/tests/media/test_generation_service.py`
- Test: `backend/tests/media/test_visual_service.py`
- Test: `backend/tests/content/test_quarantine_integration.py`

**Interfaces:**
- Consumes: media adapters, content/product/revision/material records, `ArtifactCleanupService`.
- Produces: `ContentMediaService.submit_generation`, `submit_analysis`, list/get run and safe generated `MaterialRead`.

- [ ] **Step 1: Write failing trust and file-transaction tests**

```python
def test_generation_is_bound_to_current_revision_and_plan_entry(media_service, stale_item):
    with pytest.raises(MediaStateError):
        media_service.submit_generation(stale_item.id, generation_request(stale_item.old_revision_id))

def test_generated_png_becomes_output_image_with_provenance(media_service, approved_item):
    run = media_service.run_generation(approved_item.id, approved_item.plan_entry_id)
    material = media_service.get_output_material(run.id)
    assert material.kind == "output_image"
    assert material.generation_run_id == run.id

def test_commit_ack_loss_never_creates_dangling_material_or_file(media_service, commit_then_raise):
    run = media_service.run_generation(commit_hook=commit_then_raise)
    assert media_service.prove_run_outcome(run.id) in {"succeeded", "transaction_unknown"}

def test_vision_rejects_cross_product_tampered_or_outside_material(media_service, foreign_material):
    with pytest.raises(MediaValidationError):
        media_service.submit_analysis("item-a", vision_request(foreign_material.id))

def test_model_pass_does_not_approve_revision(media_service, rejected_item):
    media_service.run_analysis(rejected_item.id, assessment(plan_match=True))
    assert media_service.content.get_content_item(rejected_item.id).status == "rejected"
```

- [ ] **Step 2: Run tests and observe RED**

Run: `python -m pytest backend/tests/media/test_generation_service.py backend/tests/media/test_visual_service.py -q`

Expected: missing media service.

- [ ] **Step 3: Implement safe image validation and cleanup-outbox persistence**

Use server-generated `content-generated/{item_id}/{run_id}/{image_id}.png|jpg|webp`. Before model call revalidate product/opportunity/evidence/revision; after model call revalidate again. Validate byte cap, MIME/magic, Pillow format, pre-load pixel count and full decode. Reserve cleanup before write; persist MaterialRecord + generation provenance + cleanup cancellation in one transaction with exact commit-ack proof.

- [ ] **Step 4: Implement bounded managed-image visual analysis**

Read the single verified file handle with an owner/remaining bound, compare hash/identity before and after, and pass encoded bytes to `VisionAdapter`. Persist strict assessment artifact/run facts; leave content/review status unchanged.

- [ ] **Step 5: Run media, content and cleanup tests**

Run: `python -m pytest backend/tests/media backend/tests/content -q`

Expected: all pass; direct-delete boundary remains clean.

- [ ] **Step 6: Commit Task 3**

```powershell
git add backend/app/features/media backend/app/features/content backend/tests/media backend/tests/content
git commit -m "feat: persist generated and analyzed content images"
```

### Task 4: Reserved worker, API and application lifecycle

**Files:**
- Create: `backend/app/features/media/api.py`
- Modify: `backend/app/api/jobs.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/health.py`
- Test: `backend/tests/media/test_media_api.py`
- Test: `backend/tests/media/test_media_worker.py`
- Test: `backend/tests/test_jobs_hardening.py`
- Test: `backend/tests/test_health.py`

**Interfaces:**
- Consumes: `ContentMediaService`, database, content service and configured adapters.
- Produces: the four media API paths in the spec and app-owned bounded worker lifecycle.

- [ ] **Step 1: Write failing API, reservation and shutdown tests**

```python
def test_generation_post_returns_202_durable_run_and_job(client, approved_item):
    response = client.post(f"/api/v1/content-items/{approved_item.id}/image-generations", json=generation_json(approved_item))
    assert response.status_code == 202
    assert client.get(f"/api/v1/content-media-runs/{response.json()['id']}").status_code == 200

def test_generic_jobs_api_cannot_complete_or_attach_media_artifact(client, media_job_id):
    assert client.post(f"/api/v1/jobs/{media_job_id}/transition", json={"state": "succeeded"}).status_code == 422
    assert client.post(f"/api/v1/jobs/{media_job_id}/artifacts", json={"kind": "generated_image"}).status_code == 422

def test_close_before_or_after_provider_call_prevents_late_success(worker, blocking_adapter):
    run = worker.submit(blocking_adapter)
    worker.close()
    blocking_adapter.release()
    assert worker.get(run.id).status != "succeeded"

def test_unconfigured_vision_and_image_are_distinct_health_facts(client):
    checks = client.get("/api/v1/health").json()["checks"]
    assert checks["bailian_vision"]["healthy"] is False
    assert checks["bailian_image"]["healthy"] is False
```

- [ ] **Step 2: Run tests and observe RED**

Run: `python -m pytest backend/tests/media/test_media_api.py backend/tests/media/test_media_worker.py -q`

Expected: missing route/worker/app state.

- [ ] **Step 3: Implement reserved jobs and bounded worker**

Reserve `content_image_generation` and `content_image_analysis`. Use single-flight per run, one bounded batch, interruptible polling and shutdown checkpoints. Provider-returned bytes/facts may persist only after a final admission and trust check.

- [ ] **Step 4: Wire API, app and health**

Use HTTP 202 for accepted runs, 404 for missing content/material/run, 409 for stale revision/open-run conflict, 422 for validation, 502 for provider failure and 503 for unconfigured adapter/database. Health reports text, vision and image capabilities separately.

- [ ] **Step 5: Run focused and full backend suites**

Run: `python -m pytest backend/tests/media backend/tests/test_jobs_hardening.py backend/tests/test_health.py -q`

Run: `python -m pytest backend/tests -q`

Expected: all controlled tests pass; guarded live media test skips without key.

- [ ] **Step 6: Commit Task 4**

```powershell
git add backend/app backend/tests
git commit -m "feat: run durable content media jobs"
```

### Task 5: Content Studio, generated-image E2E and live gate

**Files:**
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/pages/ContentStudioPage.tsx`
- Modify: `frontend/src/pages/ContentStudioPage.test.tsx`
- Modify: `frontend/e2e/fixture_app.py`
- Modify: `frontend/e2e/empty-to-package.spec.ts`
- Modify: `docs/UAT_CHECKLIST.md`
- Modify: `docs/IMPLEMENTATION_STATUS.md`

**Interfaces:**
- Consumes: media run APIs and generated output-image materials.
- Produces: operator generation/analysis/review flow and controlled end-to-end proof without a prewritten image.

- [ ] **Step 1: Write failing UI states and human-gate tests**

```tsx
it("generates each image-plan entry once and renders persisted run state", async () => { /* API fixtures */ })
it("shows model visual advice without marking the human visual check passed", async () => { /* assessment */ })
it("prevents double-click generation and supports explicit retry", async () => { /* deferred request */ })
```

- [ ] **Step 2: Implement Content Studio media controls**

Render generation and analysis state, provider/model/duration/usage, sanitized errors, produced material and advisory assessment. Preserve manual material upload as a separately labeled path. Export remains disabled until existing human checks pass.

- [ ] **Step 3: Replace the E2E prewritten image**

The controlled image adapter must return valid generated PNG bytes through the production media service. The controlled vision adapter returns strict advisory output. The browser then performs human visual checks, approves, exports and verifies ready/available ZIP.

- [ ] **Step 4: Run frontend/E2E repetition and scans**

Run: `npm test --prefix frontend`

Run: `npm run build --prefix frontend`

Run: `npm run test:e2e --prefix frontend -- --repeat-each=5`

Run: `python -m pytest backend/tests/test_release_scanner.py backend/tests/test_release_hardening.py -q`

Expected: all pass; no prewritten image in the fresh-runtime fixture; release scans remain clean.

- [ ] **Step 5: Add guarded live Bailian media smoke**

Require an explicit opt-in environment flag and locally configured Key. Perform one generated image and one visual assessment, save request IDs/usage/artifact facts, and never print credentials. Without access, report `not_run` and create no successful run.

- [ ] **Step 6: Run full verification and commit**

Run: `powershell -ExecutionPolicy Bypass -File scripts/verify.ps1`

```powershell
git add frontend docs backend/tests/integration .superpowers/sdd/2026-08-17-xhs-workbench-implementation/progress.md
git commit -m "feat: complete reviewed AI image workflow"
```
