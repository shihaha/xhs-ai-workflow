# Task 6 implementation report

Date: 2026-08-17
Branch: `feature/system-v1`
Status: implemented and locally verified; live Android smoke was not run because this machine has no ADB/device runtime available.

## Commits

- `583df3d feat: add evidence-backed Android shop collection`
- `8032b83 fix: wait for bounded Android screen transitions`

The first commit is the requested feature commit. The second records a post-commit self-review fix for bounded screen settling and a stale-detail/back-navigation race.

## Delivered scope

### Carried Task 5 rulings

- Qianfan fallback identity now requires a non-empty string `content_type`.
- A collision never disappears through `setdefault`: the first source row is accepted and every later colliding row is emitted as an explicit rejected observation with reason `duplicate_identity`.
- Rejected duplicates preserve their original raw evidence and remain included in `observed_count`.
- A `noteId` must be a scalar safe token. Object/collection values, slash-bearing values, dot segments and unsafe tokens are rejected as `malformed_note_id`.
- Platform-relative note URLs are accepted only for strict known routes with exactly one safe ID segment: `/explore/<id>`, `/item/<id>`, and `/discovery/item/<id>`.
- `/explore/../evil` and other extra/prefixed path variants are rejected instead of being turned into fabricated canonical URLs.

Changed files:

- `backend/app/adapters/qianfan_playwright.py`
- `backend/tests/radar/test_rank_ingestion.py`

### Android collection

- Added truthful ADB/uiautomator2 health reporting for missing ADB, no device, offline device, multiple devices, requested-device selection, uiautomator connection errors, wrong foreground application and ready state.
- Isolated UI selectors and activity/marker expectations in versioned profile `xhs-android-2026-08-v1`.
- Added bounded profile, shop, product detail, share, scroll and return-to-shop transitions. Delayed screens are polled within configured deadlines; a stale detail screen after Back cannot trigger an unsafe extra Back.
- Stops with `needs_human` for login prompts, captcha/security challenges and selector/layout changes. There is no login, captcha or challenge bypass.
- Checks durable job cancellation after captured transitions and does not continue copy/back interaction after cancellation.
- Captures screenshot bytes and UI hierarchy for every traversed transition, hashes the screenshot, stores both under `runtime_dir/evidence/android/<job UUID>/...`, validates containment and attaches both artifacts to the job when a job service is present.
- Ported the tutorial shop hierarchy parser for title, price, sold count, rank, discount and tap coordinates.
- Added bounded profile → shop → detail → share → copy-link → shop traversal, unique product URLs, explicit rejected observations and exact expected/observed/succeeded/missing/overflow facts.

### N/N verification and integration

- Added tutorial-compatible offline verification of:
  - unique source URLs;
  - unique, contained product directories;
  - detail link exactly matching the collection source URL;
  - at least one local product image;
  - exact image-manifest coverage;
  - SHA-256 digest equality for every local image.
- Missing expected slots and invalid discovered products are individually named; N/N completion is true only when expected, discovered and succeeded counts all agree and no issue/missing/overflow remains.
- Added durable shop collection jobs using the existing job/artifact database services; no new database table was needed.
- Added `GET /api/v1/devices` and `POST /api/v1/shop-collections`.
- Added runtime dependency declarations `adbutils>=2,<3` and `uiautomator2>=3,<4`.

Primary Task 6 files:

- `backend/app/adapters/android_device.py`
- `backend/app/features/shops/__init__.py`
- `backend/app/features/shops/service.py`
- `backend/app/features/shops/api.py`
- `backend/tests/shops/test_device_health.py`
- `backend/tests/shops/test_shop_collection.py`
- `backend/tests/shops/test_nn_verification.py`
- minimal integration in `backend/app/main.py` and `pyproject.toml`

The uiautomator2 primitives used by the adapter (`open_url`, selector `exists`/click, `dump_hierarchy`, raw `screenshot`, clipboard and Back) were checked against the project's official documentation/source: <https://github.com/openatx/uiautomator2/blob/master/README.md>.

## TDD evidence

Observed RED before implementation:

- Carried Qianfan ruling cases: 13 targeted failures under the old fallback identity, duplicate handling and URL normalization behavior.
- Initial Android/shop suite: three import/collection errors because the Task 6 modules did not exist.
- After creating test-visible placeholders: 18 behavioral failures and one pass.
- Additional focused RED regressions were observed for disconnect classification, request-scoped device selection, two-step Back navigation, cancellation during Share, delayed shop hierarchy, and stale detail hierarchy after the second Back.

Observed GREEN during implementation:

- Carried Qianfan ruling selection: 13 passed.
- Entire Qianfan rank-ingestion module: 65 passed.
- Final Android/shop module: 25 passed.
- Full backend suite: 195 passed.

## Final verification

Executed from `D:\AI_WORKSPACE\xhs-intelligence-workbench` after the final implementation changes:

```text
python -m pytest backend/tests/shops -q
25 passed in 0.80s

python -m pytest backend/tests -q
195 passed in 5.82s

python -m compileall -q backend/app backend/tests
exit 0

git diff --check
exit 0

OpenAPI assertion
/api/v1/devices present
/api/v1/shop-collections present
```

The focused Qianfan verification immediately before finalization was also clean:

```text
python -m pytest backend/tests/radar/test_rank_ingestion.py -q
65 passed in 1.08s
```

## Live smoke

Result: `not_run: device unavailable`

Observed preflight:

```json
{
  "detail": "adb_unavailable",
  "device_id": null,
  "raw_evidence": {
    "adb_executable": "adb",
    "dependency_versions": {
      "adbutils": null,
      "uiautomator2": null
    },
    "devices": [],
    "selector_profile_version": "xhs-android-2026-08-v1"
  },
  "status": "unavailable"
}
```

This is not recorded as a passing device smoke test.

## Remaining concerns / handoff

- The current Python interpreter has not been resynchronized after the dependency declaration, so neither `adbutils` nor `uiautomator2` is installed in it.
- No physical Android device, authenticated Xiaohongshu session, live clipboard result or current production selector was available on this machine. The selector profile is versioned specifically so live drift can be recorded and updated without hiding it.
- Before production use: synchronize dependencies, make ADB available, connect exactly one authorized device (or pass its device ID), bring Xiaohongshu to the foreground, complete login/captcha manually if shown, then run the opt-in real-device smoke. Device absence or a human gate must remain a factual unavailable/needs-human result.
- `D:\AI_WORKSPACE\CURRENT_PROJECT.md` was absent. The signed Task 6 brief supplied the concrete scope and completion conditions used here.
- This report path is ignored by Git and is intentionally not part of the implementation commits.

## Self-review conclusion

The final diff is limited to Task 6, the two binding Qianfan rulings, tests and minimal application/dependency integration. No known code defect remains in the locally testable scope. The only unverified boundary is the explicitly reported live Android environment.

---

## Fix round 1/5 — 2026-08-17

Commit: `473dfec fix: harden Android shop collection evidence`

### Delivered review fixes

- `POST /api/v1/shop-collections` now creates a durable queued job, returns HTTP 202 with its ID before phone work finishes, and runs the bounded collection in a managed background worker. The worker claims the job, respects queued/running cancellation, closes during application shutdown and turns unexpected background exceptions into durable failed jobs instead of stranded `running` jobs.
- Request validation uses a non-coercing `StrictInt` for `expected_count`; boolean, string and whitespace values are rejected. Account name and optional device/verification path values are trimmed and must remain non-empty.
- URL-only collection can no longer finalize success. Without deep evidence it becomes `needs_human` with `product_evidence_verification_pending`. A supplied verification directory must resolve inside `runtime_dir`, is rechecked by the worker, and must pass `verify_shop_collection()` against the exact device-observed source URLs before success.
- Detail files, image directories, each image and each manifest target are resolved and checked as contained regular targets. External symlink/junction/reparse targets and non-regular files are rejected. Manifest paths must be canonical `images/<name>` paths; traversal, dot aliases, duplicate separators and backslashes are rejected.
- A process-wide per-device reservation is keyed by the connected serial. A concurrent request for the same phone produces factual `device_busy`/`needs_human` without navigation; different serials remain parallel; every acquired reservation is released in `finally`.
- Cancellation is polled inside bounded selector/transition/clipboard waits and checked before coordinate clicks, Copy, every Back, swipe and final persistence/finalization. The final immediate-before-Back race is covered explicitly.
- Clipboard collection records the value before Copy and bounded-polls for a changed, valid XHS URL. A delayed change succeeds; an unchanged valid URL is rejected as `stale_clipboard`.
- Title deduplication was removed. Screen fingerprint plus card position is only a traversal guard; canonical source URL is the final identity. Same-title/different-URL cards both survive and duplicate URLs become explicit rejected observations with raw evidence.
- Public and persisted results retain all accepted items, all rejected rows and their raw evidence, missing rows and overflow. The complete normalized payload is stored at `runtime_dir/evidence/shops/<job_id>/result.json` and attached as `shop_collection_result` job evidence.
- The carried Qianfan rulings remain intact: fallback identity requires non-empty string `content_type`, every collision after the first is `duplicate_identity`, and unsafe/object/dot-segment `noteId` or relative routes are rejected rather than fabricated.

### Strict TDD evidence for this round

Observed RED before each implementation slice:

- Strict request validation: `4 failed, 1 passed` (boolean/string/whitespace count and whitespace names/devices were accepted).
- Asynchronous POST contract: `1 failed` (`201 != 202` and work was inline).
- Production deep-evidence integration: `1 failed` (verified evidence could not produce success); URL/evidence mismatch: `1 failed` (unrelated complete evidence was accepted).
- Nested verifier containment: `6 failed, 7 passed`; external API verification path: `1 failed` (`500 != 422`).
- Full accepted/rejected/overflow persistence: `1 failed` (public result omitted rejected observations).
- Adapter concurrency/cancellation/clipboard/title-accounting selection: `7 failed`.
- Background worker lifecycle close: `1 failed`; final cancellation persistence: `1 failed`.
- Unexpected background persistence failure: `1 failed` with uncaught `OSError`, leaving the job running.
- Immediate-before-Back cancellation race: `1 failed` because Back was still pressed.
- Noncanonical manifest aliases: `2 failed` because `images/./00.webp` and `images//00.webp` were normalized instead of rejected.

Observed GREEN after the corresponding minimal changes:

- Strict request validation: `5 passed`.
- Async response: `1 passed`; verified production integration/async selection: `2 passed`.
- Verifier URL binding and containment: `13 passed`; external verification path: `1 passed`.
- Full-result persistence: `1 passed`.
- Reservation selection: `1 passed`; remaining cancellation/clipboard/dedupe selection: `6 passed, 15 deselected`.
- Lifecycle close: `1 passed`; service final-cancellation persistence: `1 passed`.
- Unexpected worker failure: `1 passed`; immediate-before-Back cancellation: `1 passed`; canonical manifest paths: `2 passed`.
- Whole shop suite before the final self-review additions: `52 passed`; final shops plus carried Qianfan focused run: `121 passed in 2.33s`.

### Final verification for fix round 1/5

```text
python -m pytest backend/tests/shops backend/tests/radar/test_rank_ingestion.py -q
121 passed in 2.33s

python -m pytest backend/tests -q
226 passed in 6.72s

python -m compileall -q backend/app backend/tests
exit 0

git diff --check
exit 0 (line-ending conversion warnings only)

OpenAPI assertion
/api/v1/devices present
/api/v1/shop-collections present
POST /api/v1/shop-collections responses: 202, 422
```

Live device smoke remains: `not_run: device unavailable`.

Fresh observed preflight:

```json
{"detail":"adb_unavailable","device_id":null,"raw_evidence":{"adb_executable":"adb","dependency_versions":{"adbutils":null,"uiautomator2":null},"devices":[],"selector_profile_version":"xhs-android-2026-08-v1"},"status":"unavailable"}
```

This is not a passing live-device result. No ADB executable, Android device, authenticated Xiaohongshu session or live selector/clipboard behavior was available on this machine.

---

## Fix round 2/5 — 2026-08-17

Commit: `92a91c3 fix: make Android shop completion race-safe`

### Delivered review fixes

- Device-collected links and deep-verified products now have separate meanings. `collected_count` and `items` preserve canonical links, while public/persisted `succeeded_count`, job `progress_current`, `missing_count` and `complete` describe only products that passed detail/image/manifest/SHA verification. URL-only 1/1 is therefore collected 1, verified 0/1, incomplete and `needs_human: product_evidence_verification_pending`.
- Source-URL mismatch between device observations and supplied verification evidence cannot publish partial numeric success: verified success is zero, every collected URL gets an explicit `collected_source_url_mismatch` missing record, and durable progress stays zero.
- Observation accounting now exposes `raw_observation_count` and `duplicate_observation_count`. Unique canonical product URLs drive discovered/overflow N/N; overlapping A,B then B,C produces three accepted URLs, one preserved duplicate rejection, raw count four, no overflow and exact collection completion. Duplicate URL counts are inferred and validated from explicit `duplicate_source_url` rejection rows, so a producer cannot accidentally count them as overflow. Qianfan `duplicate_identity` rows are deliberately not excluded and remain in observed accounting.
- Added `JobService.finalize_running_with_artifact()`: a conditional `WHERE id=? AND state=running` update, result-artifact row and terminal state commit share one database transaction. The normalized result is written to a contained temporary file first. If cancellation wins, finalization returns without attaching a result and removes the temporary file; if finalization wins, a stale cancellation fails its conditional state update and cannot overwrite success.
- General job transitions now use conditional state updates, closing the stale ORM read/write race used by cancellation. Synchronized tests cover cancellation at both result persistence and finalization, plus the opposite race where finalization commits before a paused stale cancellation.
- On service startup, prior-process queued/running `android_shop_collection` jobs become `needs_human` with stage/error `worker_restart_required` and an explicit warning log; unsafe physical navigation is never silently resumed.
- Shutdown closes admission under a lifecycle lock, signals request-scoped cancellation callbacks, cancels queued futures, factually cancels queued/running jobs, and calls `shutdown(wait=False, cancel_futures=True)`. A blocked-adapter lifespan test proves shutdown returns without waiting for the phone worker and the queued second request never starts. A closed API returns 503 without creating a job.
- The Android adapter accepts the external shutdown cancellation callback through thread-local request context, preserving per-request isolation while keeping all existing database cancellation checks.
- Whitespace-only `selector_profile_version` now fails request validation with HTTP 422.

### Strict TDD evidence for this round

Observed RED before each implementation slice:

- Verified-vs-collected N/N and selector validation selection: `3 failed, 5 passed, 7 deselected` (URL-only/verified results had no `collected_count`; whitespace selector profile was accepted).
- Overlapping A,B then B,C: `1 failed` (`failed` instead of exact collection success). The first accounting change exposed an existing duplicate-only regression: `1 failed` (`partial` instead of the required factual result).
- Atomic cancellation/finalization selection: `3 failed, 14 deselected` (a canceled job retained an artifact and both synchronized cancellation races ended as succeeded). Direct primitive test: `1 failed` because `finalize_running_with_artifact` did not exist.
- Restart/shutdown lifecycle selection: `3 failed, 18 deselected` (old workers stayed queued/running, closed admission was not factual, and lifespan close waited about 0.375 seconds for a blocked worker).
- External shutdown callback: `1 failed` because adapter work still returned succeeded.
- Closed endpoint: `1 failed` (`500` instead of `503`).
- Verification URL mismatch semantics: `1 failed` because `succeeded_count` still reported one unverified product.
- Final contract self-review: duplicate-URL inference `1 failed, 30 deselected`; the corrected authoritative counting then exposed an inconsistent overflow fixture as `1 failed, 21 deselected`, rather than silently accepting its contradictory counts.

Observed GREEN after the corresponding minimal changes:

- Verified-vs-collected N/N and selector validation: `8 passed, 7 deselected`.
- Overlap plus Android contract selection: `31 passed`; duplicate-only regression pair: `2 passed, 23 deselected`.
- Atomic cancel/finalize races and direct stale-cancellation winner test: `4 passed, 14 deselected`.
- Restart and nonblocking shutdown lifecycle: `3 passed, 18 deselected`; external cancellation callback: `1 passed`.
- Closed service/API: `2 passed`.
- Valid verification plus source-mismatch semantics: `2 passed`.
- Duplicate-URL inference: `1 passed, 30 deselected`; affected result/contract selection after factual fixture repair: `4 passed, 49 deselected`.

### Final verification for fix round 2/5

Executed after commit-content staging from `D:\AI_WORKSPACE\xhs-intelligence-workbench`:

```text
python -m pytest backend/tests/shops backend/tests/radar/test_rank_ingestion.py -q
132 passed in 2.73s

python -m pytest backend/tests -q
238 passed in 7.03s

python -m compileall -q backend/app backend/tests
exit 0

git diff --check
exit 0 (line-ending conversion warnings only)

git diff --cached --check
exit 0
```

Fresh post-commit health preflight:

```json
{"detail": "adb_unavailable", "device_id": null, "raw_evidence": {"adb_executable": "adb", "dependency_versions": {"adbutils": null, "uiautomator2": null}, "devices": [], "selector_profile_version": "xhs-android-2026-08-v1"}, "status": "unavailable"}
```

Live device smoke remains exactly: `not_run: device unavailable`. This is not a passing Android smoke test.

### Remaining concerns / self-review

- There is still no ADB executable, physical device, authenticated Xiaohongshu session, installed `adbutils`/`uiautomator2`, or live selector/clipboard observation on this machine. Production Android behavior remains intentionally unclaimed.
- The committed diff is limited to Task 6 adapter/service/job integration and its tests. The carried Qianfan rulings remain green in the focused suite.
- No known locally testable defect remains after the round-2 race, lifecycle, count-semantics and contract review.

---

## Fix round 3/5 — 2026-08-17

Commit: `8d35e6f fix: classify interrupted Android shop jobs`

### Delivered review fixes

- Startup recovery now recognizes both the legacy durable type `shop_collection` and the current type `android_shop_collection`. Queued and running jobs of either type become `needs_human` with both `current_stage` and `error_category` set to `worker_restart_required`, plus the explicit physical-worker restart log.
- Expired-lease recovery now accepts the physical worker job types and classifies those records with `worker_restart_required` before they can be hidden behind the generic lease-expiry reason. App startup passes both legacy/current shop types. Expired non-shop jobs retain the existing generic `Running lease expired; human recovery required.` semantics, stage and error category.
- Missing-slot references now start after unique identity observations, not raw observations. With expected count 2, one accepted URL and one duplicate URL observation, the remaining slot is `expected_product:2`; the duplicate evidence still remains in raw/observed counts.

### Strict TDD evidence for this round

The three target regressions were added before production changes and run together:

```text
python -m pytest \
  backend/tests/shops/test_shop_service.py::test_service_startup_recovers_prior_android_shop_workers_as_needs_human \
  backend/tests/shops/test_shop_service.py::test_app_startup_classifies_expired_shop_workers_before_generic_lease_recovery \
  backend/tests/shops/test_shop_collection.py::test_duplicate_url_card_is_an_explicit_rejected_observation -q
```

Observed RED:

```text
3 failed in 0.76s
```

- Legacy `shop_collection` remained `queued` instead of `needs_human`.
- Expired shop work retained `device_pending` instead of `worker_restart_required` because generic recovery ran first.
- Duplicate-deficit numbering returned `expected_product:3` instead of `expected_product:2`.

Observed GREEN after the minimal fixes:

```text
3 passed in 0.66s
```

Expanded job-state plus shops regression selection:

```text
python -m pytest backend/tests/test_job_state_machine.py backend/tests/shops -q
105 passed in 3.35s
```

### Final verification for fix round 3/5

```text
python -m pytest backend/tests/shops backend/tests/radar/test_rank_ingestion.py -q
133 passed in 2.88s

python -m pytest backend/tests -q
239 passed in 7.38s

python -m compileall -q backend/app backend/tests
exit 0

git diff --check
exit 0 (line-ending conversion warnings only)

git diff --cached --check
exit 0
```

Fresh health preflight:

```json
{"detail": "adb_unavailable", "device_id": null, "raw_evidence": {"adb_executable": "adb", "dependency_versions": {"adbutils": null, "uiautomator2": null}, "devices": [], "selector_profile_version": "xhs-android-2026-08-v1"}, "status": "unavailable"}
```

Live Android smoke remains: `not_run: device unavailable`. It is not recorded as a pass.

### Remaining concern

- Physical Android navigation, authenticated state and current live selectors/clipboard remain unverified because ADB, the device and Android dependencies are unavailable on this machine. No locally testable Task 6 regression remains in this review round.

## 2026-08-20 live Android compatibility and bounded UAT

Status: physical-device collection passed its read-only navigation/link-capture boundary; independent product-image verification remains pending, so the durable result is intentionally `needs_human` rather than complete.

- Installed the official Android Platform Tools in the isolated runtime and used authorized device `V2303A` (Vivo V2303), Android 16, with the authenticated `com.xingin.xhs` app. No like, comment, follow, publish or account mutation was performed.
- Live failures first proved three current-runtime mismatches: uiautomator2 returns a Pillow screenshot by default, an unbound profile deep link can open a system browser, and Android 16/Vivo denies the legacy clipboard RPC. The adapter now accepts the supported screenshot shape, package-binds profile navigation to Xiaohongshu, and uses the uiautomator2 InputIME broadcast fallback while restoring the operator's original IME.
- A bounded three-attempt InputIME broadcast retry handles the observed receiver-start race without weakening URL trust. A single prose-wrapped share message is accepted only when it contains exactly one strict HTTPS Xiaohongshu/xhslink URL; unchanged clipboard URLs remain rejected as stale.
- Runtime `android-live-uat-20260820-08`, job `f89bdcd4-bf95-459c-a5af-57100ed97949`, deliberately failed `discovered_count_exceeds_expected`: declared 1, observed and collected 2, 16 screenshot/UI artifacts plus the result. This is retained proof that overflow cannot become fake success.
- Runtime `android-live-uat-20260820-09`, job `4689ae2b-5021-408f-affe-45ed889688de`, completed the same real ranked-account device traversal with declared/discovered/collected `2/2/2`, 16 screenshot/UI artifacts plus the result artifact. It ended `needs_human/product_evidence_verification_pending`, verified `0/2`, because no contained product-image directory was supplied. This is a successful bounded device/link smoke, not a complete external product-evidence N/N pass.
- Focused TDD captured the screenshot/navigation, restricted clipboard, transient broadcast and prose-wrapped URL cases. Fresh clean-environment shop regression: `71 passed`.
- Full backend run: `1380 passed, 3 skipped, 1 failed`. The sole failure was the pre-existing cleanup-worker child-process test loading this workstation's repo-local `.env` from its hard-coded repository cwd and rejecting the external `XHS_CLI_STATE_DIR`; all Android/shop tests passed from a clean cwd. This environment failure was not hidden and no unrelated cleanup/XHS code was changed.
- `python -m compileall -q backend/app backend/tests` and `git diff --check` passed. The device's original `com.iflytek.inputmethod/.FlyIME` was confirmed restored after the live runs.

Remaining live boundary: prepare contained image manifests for the two observed product URLs and rerun exact verification before using this shop evidence for opportunity creation.

### Same-batch verification continuation

- The two captured detail screenshots were visually confirmed as distinct real product pages, then copied into `android-live-uat-20260820-09/verification/ranked-account` with exact same-run source links and SHA-256 manifests.
- The existing strict verifier returned expected/discovered/verified/missing `2/2/2/0` and `complete=true`; no placeholder image or fabricated URL was used.
- This does not retroactively change job `4689ae2b-5021-408f-affe-45ed889688de`: its durable state remains `needs_human/product_evidence_verification_pending`. Live evidence showed why the current pre-collection verification input cannot close the job: each share pass returned different `xhslink.com` short links, and the account's visible product set also changed between runs.
- The next ranked-account evidence step was also bounded and truthful. The authenticated session probe passed; two candidate `user-posts` calls each returned five public notes, while both paired `user` profile calls failed as `cli_failed` (after one initial 20-second timeout). No profile or note database facts were written, because the trust contract requires a verified profile and does not permit synthesizing it from request input.
