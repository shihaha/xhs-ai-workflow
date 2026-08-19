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
