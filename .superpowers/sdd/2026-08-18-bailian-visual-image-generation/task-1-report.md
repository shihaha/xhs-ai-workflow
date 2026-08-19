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

At that point, real Bailian generation and vision success were still unproven. No credential or provider body was written to this report.

## Live-discovered usage projection fix

The next real run proved the corrected wan2.6 submission and polling contract: Bailian reached `SUCCEEDED` and returned a real image URL. Local processing then stopped before a managed image could succeed because the official response's usage object contains four integer counters plus dimensional metadata `size: "1280*1280"`. Passing that complete object to the deliberately numeric-only shared validator raised `BailianMediaOutputInvalid`.

The MockTransport success fixture was changed to the observed shape and failed for the same reason before production code changed. The image client now selects only `image_count`, `input_tokens`, `output_tokens` and `total_tokens`, then passes those values through the unchanged strict numeric validator. It ignores `size`; it does not loosen validation or accept arbitrary usage value types.

```text
python -m pytest backend/tests/media/test_bailian_media.py::test_image_generation_polls_bounded_task_and_validates_real_png -q
1 passed in 0.17s

python -m pytest backend/tests/media -q
57 passed in 13.63s
```

The next complete real run must still prove managed image download/persistence and visual analysis. No live pass is claimed yet.

## Live-discovered vision schema-instruction fix

The third real run proved the complete image path: wan2.6 submission, polling, `SUCCEEDED`, remote download, byte validation and managed material persistence all completed. The subsequent qwen-vl-max call returned syntactically valid JSON, but used self-selected Chinese keys such as `匹配度`, `文字可读性`, `明显瑕疵` and `安全问题`. Strict `VisualAssessment` rejected that object, so real vision success is not claimed.

The cause was request construction, not Pydantic: `response_format=json_object` requires JSON syntax but does not define field names. The vision request previously sent only the assessment prompt and no schema contract.

TDD RED showed the outbound messages lacked a system schema instruction. The minimal fix prepends a fixed system message that:

- explicitly requests JSON only;
- embeds the exact `schema.model_json_schema()` object;
- requires exact property names and types;
- forbids translated, renamed and additional keys.

The caller cannot supply or replace that system instruction. Response validation remains the same strict Pydantic path; no Chinese-key mapping or permissive parser was added.

```text
python -m pytest backend/tests/media/test_bailian_media.py::test_vision_uses_only_configured_model_and_returns_advisory_assessment -q
1 passed in 0.15s

python -m pytest backend/tests/media -q
57 passed in 12.84s
```

Real image generation is now proven through managed material. Real visual analysis remains failed/pending until a vision-only or complete live rerun passes.

## Live-discovered vision usage projection fix

The next vision live attempt proved the schema instruction worked: qwen-vl-max returned an object that passed strict `VisualAssessment`. Local completion then failed on the official OpenAI-compatible usage envelope, which contains integer `prompt_tokens`, `completion_tokens` and `total_tokens` plus nested `prompt_tokens_details` and `completion_tokens_details`. The shared validator is intentionally numeric-only and correctly rejected the nested dictionaries.

MockTransport reproduced that exact usage shape before the fix. The vision adapter now selects only the three official numeric counters and passes them to the unchanged shared validator. Nested detail objects are ignored; arbitrary types are not accepted. Text and image adapters were not changed, and no additional provider shapes were added.

```text
python -m pytest backend/tests/media/test_bailian_media.py::test_vision_uses_only_configured_model_and_returns_advisory_assessment -q
1 passed in 0.17s

python -m pytest backend/tests/media -q
57 passed in 13.45s
```

Real image generation remains proven. The vision schema is now proven against the live provider, but durable visual completion still requires one vision-only rerun after this usage fix.
