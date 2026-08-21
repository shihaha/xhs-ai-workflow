# Phase A isolated real UAT report

Date: 2026-08-20 (Asia/Shanghai)

## 2026-08-21 bounded provider verification and current review state

Analysis `5d4e235a…` remains the exact historical failure: its three model
attempts each reached the configured 60-second timeout. It has no output,
usage or Opportunity. Those facts show a provider timeout, not three negative
business judgments.

The user then authorized one bounded verification. A temporary local service
used the same runtime database and current code with a 120-second timeout and
`max_attempts=1`. Analysis `5859e6c9…` received a valid response on that single
request after 45,797 ms. Bailian `deepseek-v4-flash` reported 243,276 prompt
tokens and 4,984 completion tokens under prompt
`tutorial-demand-radar-specific-demand-v2`.

The saved conclusion states `has_specific_shared_demand=true` and names the
common demand as “七宗罪与人格心理测试数字内容”. It created one current
`warming_candidate + pending_review`, “七宗罪心理测试数字内容市场机会”. The
candidate cites both current clean shop facts and account-bound note evidence.
SQLite and the main backend service returned the same analysis and Opportunity.

No human review decision has been recorded. The earlier `73728a7c…` candidate
uses a legacy shop fact that is now ineligible, and the older `37d7fca6…`
candidate lacks the current positive specific-demand contract; neither can pass
the current approval gate. They remain visible as immutable audit history.

The review UI initially failed because its port-8000 backend process was stale
and returned HTTP 500 for `/analyses`; port 8001 and SQLite remained healthy.
Restarting port 8000 against the current runtime restored all four review-page
reads. Browser verification found the current title and common demand, no load
error and no console error. The observed system-status and radar error text is
now Chinese; all 65 frontend tests and the production build pass.

Phase A now waits only for explicit human disposition of the current candidate.
Approval would complete the Phase A review gate, not start product work. Phase B
remains unstarted and requires separate user authorization.

## Historical checkpoint: 2026-08-21 final ranked-pool and clean-evidence result

Positions 62–71 were run one at a time in original Qianfan score order. All ten
were already `uncertain` at prescreen and all ten finished Android preflight as
`needs_human 0/3`. No new in-scope account, profile collection, note collection
or evidence sample was therefore warranted. The final funnel is 2 in-scope, 31
physical, 36 needs-human, 2 collection failures and 0 waiting.

Readback exposed that position 10 still had one opportunity-eligible legacy
full-shop result with eight SHA-bearing XML files whose current bytes differed.
After a lock-screen attempt was preserved as `needs_human`, clean replacement
job `8507e249…` succeeded at proven natural-end 2/2 with 25 artifacts and 24/24
SHA matches. Position 61 uses clean job `fb293aa0…`, with 23 artifacts and 22/22
SHA matches. A RED/GREEN trust gate now rejects a shop result when any
SHA-bearing artifact from that job differs and repeats the trust check before
human approval. The old files and metadata remain unchanged.

Analysis `73728a7c…` ran before that gate and produced one pending personality-
test candidate from the now-ineligible legacy shop result. It remains immutable
audit history and cannot pass the current approval trust check. The subsequent
clean-only analysis `5d4e235a…` used 2 accounts and 22 eligible facts but failed
closed after the provider exhausted three attempts (`model_retry_exhausted`). It
was not loop-retried and created no Opportunity.

The old newline mismatch is formally closed as
`legacy_historical_audit_limitation`: exact-written-byte hashing is covered and
used by current production jobs, both current eligible shop facts are clean,
and old immutable artifacts remain for audit. It is non-blocking. At this
checkpoint, Phase A remained incomplete because the clean-only unified provider
run failed. The later bounded provider success is recorded above; Phase B
remains unstarted.

## Historical checkpoint: 2026-08-21 short-shop evidence and CDP result

The formal sample target remains three for shops that have at least three
products. A same-run Android natural-end marker may now prove that a shop has
only one or two products; only then does the exact target become the proven
available count. Every available item must still have same-job detail,
distinct source identity, image, manifest and SQLite evidence. Partial results
without that proof remain ineligible.

RED tests reproduced both short-shop failures and the Windows XML byte-hash
mismatch. The minimal production change passed the new one/two-product tests
and the existing three-product path. New CDP job `7c11ae26…` then persisted a
latest-10 account sample. Final Android job `fb293aa0…` truthfully persisted
two observed products, `natural_end_reached=true`, and a successful 2/2 evidence sample with two discoveries, two stable identities, two distinct
HTTPS sources, `sample_complete=true` and `shop_complete=false`.

All 23 Android artifacts exist. All 22 artifacts carrying SHA metadata match
their current bytes, including ten XML hierarchies; five hierarchies retain the
natural-end marker. Manifest and collection hashes match. After service
restart, the shop remained 2/2 and the account profile plus ten notes remained
readable. Clean replacement job `8c369a80…` also succeeded for the other
qualified account at 3/3 with 33/33 SHA-bearing artifacts matching current
bytes. Historical failed jobs and their byte mismatches were not rewritten.

Unified analysis `26979db8…` consumed the two clean shop facts and twenty
latest-note facts. It succeeded with two demand profiles,
`has_specific_shared_demand=false`, `common_demand=null` and
`opportunities=[]`; the Opportunity table did not grow. Restart readback
preserved both evidence eligibility and the analysis. The current Phase A
two-account decision loop is complete without a candidate. Phase B remains
unstarted.

## Historical checkpoint: 2026-08-21 connected-phone continuation result

After the phone returned as an authorized ADB device, Xiaohongshu was launched
and verified in the foreground. The ranked funnel continued from position 18,
one terminal job at a time. No previous disconnected job was retried or
rewritten, and no category or desired demand was supplied to candidate
selection.

The run reached position 61. Across the complete 71-candidate projection, 2 are
now final in-scope, 31 physical, 26 needs-human, 2 collection-failed and 10
waiting. Accounts without a verifiable shop entry stayed needs-human. One shop
that failed on its third item stayed at 2/3; no fourth item replaced it.

Position 61 naturally produced the second final in-scope account. Preflight job
`151a887f…` succeeded after two representative products with 25 artifacts. The
next profile/latest-10 job `d3a4d2d1…` failed closed as `cdp_unavailable`.
Formal evidence-sample job `77fedee2…` persisted two real product discoveries
and their detail/share evidence, then stopped because the third required item
was missing. Its durable result is partial, `expected_products_missing`, 2/3,
`sample_complete=false` and `shop_complete=false`. It is not opportunity-
eligible, so no cross-account analysis or Opportunity was created.

All 49 referenced artifact files for the gate, XHS attempt and evidence sample
exist. Of 47 artifacts with SHA metadata, 27 match current bytes and 20 Android
XML hierarchy files reproduce the known Windows newline/hash mismatch. Restart
readback preserved every terminal state and the two-account in-scope funnel.
This historical checkpoint remained open at the time and is superseded by the
short-shop and unified-analysis result above. Phase B remains unstarted.

## 2026-08-21 candidate business-scope funnel result

This increment does not change the tutorial scoring formula. It adds a low-cost,
evidence-bound screen after score ordering and before Android work. The screen
can say likely digital, clearly physical or uncertain; only clearly supported
physical accounts are skipped. Likely digital and uncertain remain Android
unknown. Android scope is still final, and prescreen artifacts are not accepted
as shop or Opportunity evidence.

The account candidate sequence can continue past the first 20 entries, but it
cannot accept a category or matching target. Exclusion and terminal collection
failure advance to the next original score position. This account replacement
does not change the separate single-shop rule of exactly the first three
distinct products encountered, without skipping or fourth-item replacement.

The real 2026-08-20 pool contained 71 score-ordered candidates. One authorized
prescreen pass persisted 4 clearly physical, 2 likely digital and 65 uncertain
results. Direct SQLite/filesystem validation found 71 artifact rows, 71 complete
payloads and 71 valid current-file/persisted SHA-256 bindings. After process
restart, the API returned all 71 classifications. Existing Android decisions
remained final: the merged funnel showed 1 in-scope, 9 physical, 7 needs-human
and 54 waiting candidates before new continuation attempts.

The next two eligible candidates were positions 8 and 9. Each was queued only
after the prior request had already terminated, and each immediately persisted
`failed/device_disconnected`. Direct ADB output listed no device and mDNS found
no service, so no third attempt was made. No old job, artifact, analysis or
Opportunity was rewritten. No new account profile, note sample, product evidence
sample, cross-account analysis or Phase B action followed.

Verification passed: Radar 112, focused Android/shop 33, analysis 178 with one
conditional skip, frontend 65, and the production frontend build. The live
prescreen and restart trust chain are proven; live ranked Android replacement
and a larger qualified digital-account pool remain blocked by the absent phone.
Phase A is not declared complete.

The additional whole-backend run was not fully green: 1498 passed, 3 skipped
and 5 failed. One failure is an unchanged media token-usage expectation; four
are unchanged legacy/shop-scope expectations. None enters the modified Radar
funnel or its focused Android path. They remain recorded and were not used to
justify unrelated code changes.

## 2026-08-21 specific-demand semantic result

The persisted output of analysis `37d7fca6…` contained two separate product
clusters (`尾单服装配饰` and `美食优惠券`) but created an Opportunity because both
accounts promoted shop products through Xiaohongshu notes. The provider did not
identify one shared concrete customer problem. The existing prompt, schema and
service validated citation truth, evidence ownership and account coverage but
had no specific-demand semantic contract.

General RED tests used unrelated home-organization and professional-exam
accounts. The GREEN contract requires one demand profile per account (offering,
target user, motivation/problem, delivery format and use scenarios) and one
cross-account conclusion (specific shared demand, commonalities, differences,
rationale and evidence). Broad umbrella needs and shared channels/marketing
methods are explicitly insufficient. A negative conclusion cannot carry an
Opportunity. Existing evidence grounding was not relaxed.

A bounded independent review found and closed two remaining Important gaps:
the final conclusion must cite every requested account, and candidates whose
parent analysis lacks a positive specific-demand conclusion cannot be approved
through either the API or UI. They may still be rejected without rewriting
history.

Backend analysis regression is `178 passed, 1 skipped`; frontend regression is
`63 passed`; the frontend build passes. One valid real provider call reused the
same two accounts and same 22 evidence IDs. Analysis `3322dda1…` succeeded under
`tutorial-demand-radar-specific-demand-v2`, concluded that low price was only a
broad strategy across unrelated food-coupon and clothing-tailstock needs, and
persisted `has_specific_shared_demand=false`, `common_demand=null`, and
`opportunities=[]`. SQLite contains no Opportunity linked to the new analysis.
Its trust fingerprint matches the historical analysis and restart readback is
consistent. Analysis `37d7fca6…` and its `pending_review` Opportunity remain
unchanged. Phase B was not started.

## 2026-08-21 grounded candidate result

The failed analysis `28856ff2…` had passed provider-side strict schema parsing
and failed only in the service's evidence-grounding layer. Its rejected output
was not persisted, so the exact offending field cannot be reconstructed. A RED
test showed that the prompt omitted the validator's account-coverage,
evidence-ownership and support-citation contract; the minimal GREEN change
exposes those existing rules without relaxing validation. The focused analysis
file passes `70/70`.

One authorized real call then reused the same two accounts and 22 trusted facts.
Analysis `37d7fca6…` succeeded and persisted exactly one
`warming_candidate + pending_review`. Its support covers both accounts, two
trusted shop facts and twenty trusted note facts, and every support ID is also
present in the candidate citations. The candidate remains unreviewed. No Phase
B action was run. The Phase A demand-validation milestone is proven, while
overall UAT remains open for explicit human review and the historical XML
current-byte hash limitation.

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
