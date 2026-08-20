# Phase A isolated real UAT report

Date: 2026-08-20 (Asia/Shanghai)

## Verdict

Phase A software is implemented, but the real Phase A UAT is **not passed**.
Phase B has not started. The current blocker is no longer the original generic
`cli_failed`: the XHS control path was restored and a second account reached a
trusted profile plus a bounded 10-note sample, but its 18 observed Android shop
links were never durably persisted. The current truthful product sample is
therefore `0/3`, not `3/3`.

## Final requested status

1. **`cli_failed` root cause**: the isolated live run copied credential state
   without the complete trusted CLI/private-runtime state required by the
   pinned read-only wrapper. The failure occurred before trustworthy profile
   completion and affected both candidates and the known control account.
2. **What was fixed**: the isolated prepared-state path, pinned read-only CLI
   wrapper, bounded process budget, real `user`/`user-posts` response handling,
   latest-account-note sampling, Android overlapping-card deduplication, shop
   scope preflight and bounded three-product evidence contracts were fixed.
   Trust/evidence checks were not relaxed and historical failures were retained.
3. **`real-account-A` control**: restored successfully. Production trust readers
   can read one profile and 62 persisted public notes. Its trusted shop result
   remains expected/discovered/succeeded `2/2/2`, missing `0`, `complete=true`.
4. **Second real account**: partially successful. One distinct candidate reached
   one trusted profile plus 10 persisted latest-note sample facts. It did not
   reach a trusted shop result.
5. **Second-account counts**: notes `10`; trusted products `0`. Android observed
   18 deduplicated product links in process memory, but the discovery helper did
   not persist the returned product items.
6. **Real cross-account analysis**: not run. The service correctly refused to
   proceed without trusted shop evidence for both accounts.
7. **Real candidate**: none produced; no common demand was fabricated.
8. **Candidate state**: no real `evidence_level` or `review_status` exists because
   no candidate row was created.
9. **Human review**: not run; there was nothing eligible to approve or reject.
10. **Git handoff**: the final local and remote SHA are recorded in the delivery
    response after the documentation commit and push.

## Durable evidence reconciliation

The historical Android discovery job that observed 18 unique links remains
`needs_human / expected_count_unknown`. Its durable evidence contains exactly
132 files and database artifact rows:

- 66 `android_screenshot` artifacts;
- 66 `android_ui_hierarchy` artifacts;
- 0 `shop_collection_result` artifacts;
- 0 artifact metadata rows containing reusable `source_url` fields;
- 0 job-input or job-log rows containing reusable product links.

All 132 declared evidence files exist. However, there is no candidate
`collection.json`, product-link result file, image manifest or normalized SQLite
product result. The only shop `result.json` elsewhere in the isolated runtime
belongs to the already completed two-product account and cannot be reused.

The earlier statement that the 18 links were “preserved” was inaccurate. The
screenshots and UI hierarchies were preserved; the links themselves remained
only in the process-local `CollectionResult`. The helper printed counts and
transitioned the job to `needs_human` without attaching that result. The system
will not rescan the phone or substitute different products merely to make the
UAT pass.

## Software delivered after the original handoff

- Account homepage collection is an engineering-bounded latest-10 sample; it
  is not described as full-account completeness or as a tutorial requirement.
- Keyword search remains a separate tutorial-aligned bounded workflow.
- Android card traversal rejects fixed-toolbar cards and deduplicates adjacent
  viewport overlap without trusting changing share short-links.
- Default shop work starts with a maximum-three-product scope preflight.
- The current clothing/physical-goods account is `out_of_scope_physical` and
  has exited the real Phase A candidate set. Its profile, latest-10 notes, raw
  Android evidence and historical `needs_human` job remain unchanged.
- `bounded_sample/test_override` is test/debug/preflight-only. It cannot qualify
  as real Opportunity evidence even when its controlled 3/3 files are exact;
  it must still state `sample_complete=true`, `shop_complete=false`.
- Bounded sample result files bind controlled `manifest.json` and
  `collection.json` paths and SHA-256 values. Analysis accepts this exception
  only when every approved 3/3 field, job state, result file, manifest, collection
  file and source-URL ordering remains exact for diagnostic reads only.

These software capabilities do not convert the missing historical links into a
real 3/3 result.

## Verification already completed

- Shop scope/bounded focused backend: 8 passed.
- Shop service isolated regression: 32 passed.
- Bounded shop analysis focused: 9 passed.
- Account-page/API frontend focused: 23 passed.
- Frontend full unit suite: 60 passed.
- Frontend production build: passed.

These are software verification results, not proof of real Phase A completion.
No additional full-repository run was required for this truthful blocked-state
handoff.

## Stop condition

Current real status is `sample 0/3`, `shop_complete=false`. Phase A remains
blocked at durable second-account shop evidence. No cross-account analysis,
candidate, review, product workflow or Phase B activity was started.
