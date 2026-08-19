# SDD ledger — plan: docs/superpowers/plans/2026-08-18-bailian-visual-image-generation.md

## Preflight rulings

| Interface | Ruling |
|---|---|
| Text / vision / image configuration | Separate trusted Settings fields and models; only the API key is shared. Request contracts expose no endpoint, model, URL or output path. |
| Vision output | Strict advisory `VisualAssessment` only. It has no review/approval mutation capability. |
| Generated image output | Return bytes only after MIME agreement, supported-format decode, full load, dimensions, pixels, bytes and digest are proven. |
| Bailian asynchronous image task | Fixed service route and configured model, bounded HTTP retries, terminal-status allowlist, polling deadline and poll-count cap. |
| Provider facts | Store only model, bounded usage, request/task IDs, prompt version and sanitized attempt categories; never provider bodies, headers or API keys. |
| Live validation | Not run in Task 1. No key was requested or used. |

Task 1: implementation complete; independent review pending. TDD RED was the expected missing-contract/module import failure. Focused GREEN: 24 passed. Full backend regression: 1321 passed, 2 guarded live skips; no failures. Live Bailian remains not_run.

Task 2: implementation and review fix round 1 complete; focused rereview pending. TDD RED covered the missing media persistence package, terminal/read-schema behaviors and the review-proven raw failure-text persistence. Failure transitions now map only a fixed safe category allowlist and fixed sanitized detail; caller exception/category text is never stored. Latest focused/necessary backend verification: 45 passed. Earlier full backend regression: 1338 passed, 2 guarded live skips; no failures. Live Bailian remains not_run.

Task 3: pending.

Task 4: pending.

Task 5: pending.
