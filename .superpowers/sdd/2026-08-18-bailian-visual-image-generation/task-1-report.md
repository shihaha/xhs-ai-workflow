# Task 1 report — media contracts and Bailian clients

## Scope delivered

- Provider-neutral `VisionRequest`, `VisionResult`, `ImageGenerationRequest`, `GeneratedImage`, `VisionAdapter` and `ImageGenerationAdapter` contracts.
- Strict advisory `VisualAssessment`; unknown fields such as model-supplied approval are rejected.
- Independent trusted text, vision and image endpoint/model/retry/timeout settings.
- A bounded OpenAI-compatible Bailian vision adapter using only supplied managed image bytes.
- A bounded Bailian asynchronous image adapter using a fixed text-to-image route, configured model, terminal status allowlist and deadline/count-limited polling.
- Credential-free generated-result download plus MIME/magic/full-decode/dimension/pixel/byte/digest validation.
- Sanitized auth, 429, 5xx, timeout, malformed schema and invalid image failures.

No database, worker, API, frontend, XHS or research files were changed. No live API call was made.

## TDD evidence

Initial RED:

```text
python -m pytest backend/tests/media/test_adapter_contracts.py backend/tests/media/test_bailian_media.py backend/tests/test_settings.py -q
ERROR backend/tests/media/test_adapter_contracts.py
ImportError: cannot import name 'GeneratedImage'
ERROR backend/tests/media/test_bailian_media.py
ModuleNotFoundError: No module named 'backend.app.adapters.bailian_media'
```

Focused GREEN after implementation:

```text
python -m pytest backend/tests/media backend/tests/test_settings.py -q
........................                                                 [100%]
24 passed in 0.19s
```

The wan2.6 payload compatibility test separately failed at `1024*1024` and passed after using the documented supported `1280*1280` size. The generated-image transient-download test separately failed on the first 429 and passed after adding credential-free bounded retry.

## Final verification

```text
python -m pytest backend/tests -q
1321 passed, 2 skipped, 40 warnings in 236.72s (0:03:56)
```

The skips are guarded live checks. The warnings are the existing SQLAlchemy/Python 3.12 SQLite datetime deprecations in content tests; there were no failures.

```text
python -m py_compile backend/app/adapters/contracts.py backend/app/adapters/bailian_media.py backend/app/settings.py
git diff --check
```

Both commands exited successfully.

## Live-discovered wan2.6 compatibility fix

The first opt-in real generation attempt on 2026-08-19 reached Bailian but did not create a successful image run. Bailian returned HTTP 400 `InvalidParameter` with `url error`; its request ID was retained in the local live output. Vision was not reached. The cause was specific and reproduced: the client used the wan2.5-and-earlier `/services/aigc/text2image/image-synthesis` + `input.prompt` contract for the configured `wan2.6-t2i` model.

TDD RED:

```text
python -m pytest backend/tests/media/test_bailian_media.py::test_image_generation_polls_bounded_task_and_validates_real_png -q
FAILED: actual POST .../services/aigc/text2image/image-synthesis
expected POST .../services/aigc/image-generation/generation
```

The minimal fix changes only the wan2.6 submission route/body and success URL extraction. It now uses `input.messages[0].content[0].text` and reads the one requested image from `output.choices[].message.content[].image`. Polling remains `/tasks/{task_id}`.

Focused GREEN:

```text
python -m pytest backend/tests/media/test_bailian_media.py::test_image_generation_polls_bounded_task_and_validates_real_png -q
1 passed in 0.13s

python -m pytest backend/tests/media -q
57 passed in 12.37s
```

Necessary backend regression evidence:

```text
python -m pytest -c <repo>/pyproject.toml <repo>/backend/tests -q
1361 passed, 3 skipped, 1 failed in 291.60s
```

The single failure is not in Bailian/media code: a cleanup-worker subprocess deliberately sets its cwd back to the repository, loads the locally prepared live `.env`, and rejects that `.env`'s external `XHS_CLI_STATE_DIR` against the test's temporary runtime. A separate combined run showed the same local live setting contaminating three direct Settings tests. The `.env` file was not printed, modified or disabled; only its variable names were checked to establish the cause. The complete media suite is green.

Real Bailian generation and vision success remain unproven until the opt-in gate is rerun. No credential or provider body was written to this report.
