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

Live Bailian vision and image generation remain `not_run`; Task 1 used controlled HTTP transports and real in-memory PNG bytes only.
