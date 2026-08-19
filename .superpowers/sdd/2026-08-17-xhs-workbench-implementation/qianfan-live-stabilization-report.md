# Qianfan authenticated live stabilization report

Status date: 2026-08-20

## Implemented software contract

- The code-owned supported profile is `qianfan-note-rank-live-v1` for the
  verified note-rank layout. It includes four board selectors, two dimension
  selectors, active-selector checks, exact response endpoint paths, `sortBy`
  board mappings, `noteType=0`, and canonical `pageNo=1,pageSize=10`.
- A scope accepts only an HTTP/business-successful POST response whose path,
  allowlisted request facts, and currently active UI selectors agree. The
  page-size-one helper response is retained only as sanitized capture context;
  it cannot become a verified ranking response.
- Content and account ranking endpoints normalize separately. Account rows use
  safe `userId` identities and public profile URLs without inventing note
  fields.
- Captured request evidence contains only method, path, `sortBy`, `noteType`,
  `pageNo`, and `pageSize`; response URLs omit query/fragment. Credential-like
  object fields, including `xsec_token`, are recursively removed before raw
  evidence is returned or persisted.

## TDD evidence

RED: before implementation, `pytest backend/tests/radar/test_qianfan_live_stabilization.py -q` produced **2 failed**. The default profile was deliberately unsupported, so the canonical request-bound content path could not succeed and an account endpoint returned no normalized item.

GREEN: the same focused stabilization suite later produced **7 passed in 0.29s**. It covers canonical `pageSize=10` selection, helper-page exclusion, request scope mismatches (`sortBy`, `noteType`, page number, page size), account normalization, profile completeness, and nested token redaction.

## Live UAT boundary

This is software-contract evidence only. No authenticated Qianfan browser run,
no eight-scope collection, and no platform write was performed in this change.
The controller must run the isolated authenticated eight-scope gate with
`expected_count_per_scope=10` and record the resulting job IDs, evidence paths,
and 8/8 persisted scope facts before live success can be claimed.

## Fix round 1 evidence

The authenticated production-UAT collection beginning `75ca2ef5` reached all
eight scopes as `needs_human/layout_changed`. Its artifacts had the correct
ranking-page URL, no captured responses, and no `capture_errors`. This exposed
an early readiness check immediately after `domcontentloaded`, not a verified
layout break or collection success.

RED: before this fix, `pytest backend/tests/radar/test_qianfan_live_stabilization.py -q` produced **2 failed**: a page whose ready selector appeared during the configured timeout was rejected before any wait, and an invalid JSON response retained credential-like text in `raw_text`.

GREEN: `pytest backend/tests/radar/test_rank_ingestion.py backend/tests/radar/test_qianfan_orchestration.py backend/tests/radar/test_qianfan_live_stabilization.py -q` produced **91 passed in 10.34s**. Scope readiness now polls login/captcha/ready within the existing deadline, clicks only after ready, and returns `layout_changed` only after that bounded timeout. JSON parse failures retain only the error category and body length; no response body text is persisted. No live browser run was performed for this fix.

## Fix round 2 evidence

The second authenticated production-UAT collection beginning `e6f2ffef` also
did not succeed: all eight scopes reached `scope_unverified`, with active UI
selectors true and 2–6 HTTP-200 ranking responses per scope. Persisted request
facts contained only method/path, so no response could meet the request-bound
scope contract. It produced **0 snapshots**; credential-hit count was **0**.
The first collection (`75ca2ef5`) remains `layout_changed`; neither run is a
live pass.

RED: `pytest backend/tests/radar/test_qianfan_live_stabilization.py -q` produced **1 failed, 10 passed** when a response used the real Playwright Python `post_data_json` property shape. The adapter called it as a function, caught the resulting error, and silently omitted `sortBy`, `noteType`, `pageNo`, and `pageSize`.

GREEN: the adapter now accepts either a property value (the production
contract) or the existing callable test double, while retaining only the
allowlisted request facts. The focused Qianfan suite produced **92 passed in
10.56s**. No live browser run was performed for this fix.
