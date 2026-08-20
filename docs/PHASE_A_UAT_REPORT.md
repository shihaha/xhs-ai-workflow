# Phase A isolated real UAT report

Date: 2026-08-20 (Asia/Shanghai)

## 2026-08-21 approved evidence-sample result

The user approved a general three-product evidence rule for large in-scope
shops. `evidence_sample` takes the first three distinct products in the default
shop order, forbids selection/skipping/fourth-product substitution, requires a
trusted in-scope preflight and exact same-job detail screenshot, source,
manifest, SHA and SQLite bindings, and remains explicitly
`sample_complete=true` / `shop_complete=false`. The old
`bounded_sample/test_override` remains ineligible for real opportunities.

Real job `84f11a0d…` succeeded at `3/3`, missing `0`, stage
`shop_evidence_sample_complete`, with 34 artifacts and persisted manifest and
collection hashes. A fresh Database/AnalysisService read accepted its result as
opportunity-eligible evidence. One subsequent real two-account analysis used
two trusted shop facts and twenty trusted latest-note facts. Analysis
`28856ff2…` failed closed as `evidence_grounding_failed`; it created zero
opportunities and was not automatically retried. Phase A remains not passed and
Phase B has not started.

## 2026-08-21 dynamic-card live result

The latest authorized real-phone validation succeeded at exact `3/3` with no
rejected product. The adapter waited for two consecutive stable product/bounds
samples after scrolling, relocated the same title/price signature immediately
before clicking, and retained one bounded same-target retry behind an explicit
detail-page postcondition. The durable job `5319ba52…` has 33 bound artifacts:
15 screenshots, 15 UI hierarchies and three discovery JSON files.

A fresh Database/JobService read returned `succeeded`, progress `3/3`, the same
33 artifacts, discovery order `[1,2,3]` and three distinct source identities.
Focused tests are `5 passed`; the complete shop collection file is `40 passed,
1 failed`, where the remaining failure is the pre-existing service scope
expectation outside this adapter path.

All screenshot and discovery JSON current-byte hashes match SQLite metadata.
The XML metadata hashes still represent the pre-write hierarchy and only match
after reversing the known Windows newline expansion, not the current file
bytes. This limitation is not hidden. This result supersedes the older
"third product not verified" statement below without rewriting any historical
failed job. Phase A as a whole remains not passed; no cross-account analysis,
Opportunity, candidate review or Phase B action was run.

## Verdict

Phase A software is implemented, but the real Phase A UAT is **not passed**.
Phase B has not started. The current blocker is no longer the original generic
`cli_failed`: the XHS control path was restored and a second account reached a
trusted profile plus a bounded 10-note sample, but its 18 observed Android shop
links were never durably persisted. The current truthful product sample is
therefore `0/3`, not `3/3`.

### 2026-08-21 bounded Android selector repair

The retained shop XML proves that the two-column product grid is emitted in
column-major DOM order. The old parser preserved that order, while overlap
comparison assumed visual row order. After a scroll, the prior `A,B` viewport
was parsed as `A,C,B,D`, overlap became zero, and the third observation clicked
the repeated `A` title coordinate. The captured post-click screen remained the
shop list. The minimal repair sorts parsed cards by vertical center and then
horizontal center; the retained XML now yields overlap two and selects the
next distinct visible card coordinate.

A real-layout regression was observed RED before the production change and
GREEN after it. The existing overlapping-viewport regression also remains
GREEN. Focused shop collection verification was `36 passed, 1 failed`; the
unfiltered shop suite was `109 passed, 4 failed`. The four failures
are existing scope/legacy service expectations and do not enter the Android
parser/click path.

The historical `d21d3e81…` job remains unchanged at `needs_human`, `2/3`,
`selector_changed`, with 22 artifact rows and two restart-readable discoveries.
The one permitted new validation job `fe7b58b5…` failed at `0/3` before any
device click because the one-off runner passed the job ID through an unsupported
keyword argument. It has zero artifacts and was not rewritten or retried; no
second job was created. Therefore the real third product is **not verified**.

Current-byte hashes match SQLite metadata for the historical ten screenshots
and two discovery JSON files. The ten XML metadata hashes describe the original
hierarchy text but do not match the on-disk bytes after Windows newline
expansion. This pre-existing evidence-chain gap was not changed in the bounded
click repair. Phase A remains not passed; no cross-account analysis,
Opportunity, candidate-review state, or Phase B work was run.

### Latest bounded continuation

The Windows account-read route is now a dedicated visible Chrome collection
profile with manual login and localhost CDP. A production
`XhsCollectionService` control for `real-account-A` succeeded with one profile,
latest-10 notes and one trusted artifact; a fresh Database/Service instance read
the same ten notes with exact owner and job binding. One additional ranked
candidate then persisted a trusted profile and latest-10 sample. Its Android
scope preflight stopped truthfully at 2/3 representative products with
`needs_human/selector_changed` and 26 Android artifacts. No selector retry,
sample expansion, cross-account analysis or candidate fabrication followed.
The real Phase A verdict therefore remains **not passed**.

Verification for this continuation: CDP/settings/registry/collection focused
tests `48 passed`; frontend `60 passed` and production build passed; Python
compile, dependency audit, secret scan and boundary scan passed. The clean-cwd
full backend run was `1451 passed, 3 skipped, 5 failed`. The five known failures
are outside this CDP change: one Bailian image-usage expectation and four legacy
shop/preflight expectation tests. They were not expanded into this bounded task.

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

## 2026-08-20 physical-scope correction and bounded continuation

The account-level delivery-scope decision is now durable and evidence-bound. The
previous REPor Android job remains `needs_human/selector_changed`; a separate
human decision records `out_of_scope_physical`, cites its two persisted discovery
artifacts, survives a database/service restart, and prevents future Android work
for the same account.

Focused verification passed: 31 scope/service tests and 3 Android preflight
boundary tests, plus compile and diff checks. A clean-cwd run of the two complete
shop test files produced 89 passes and the same four pre-existing
legacy/preflight expectation failures already recorded before this correction.

The ranked continuation inspected 17 previously unprocessed accounts before the
run was stopped for exceeding the original five-candidate bound. Eight accounts
were deterministically excluded after the first physical product; one additional
physical decision was persisted despite a later truthful selector failure. One
account produced explicit digital-delivery evidence, but its full-shop discovery
stopped at 2 products with `needs_human/selector_changed`, so no N/N shop evidence
or qualified account was created. Other accounts failed closed at the bounded
account sample or Android entry. No cross-account analysis ran, and no opportunity
or pending review was fabricated.
