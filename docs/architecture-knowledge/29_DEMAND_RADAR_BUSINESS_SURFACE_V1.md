# 29 — Demand Radar Business Surface V1

Status: **Stage 7.1 + 7.2 formally accepted on Draft PR #24; dedicated and cumulative CI green**

Date: 2026-08-24

## 1. Scope

This slice implements only the first two Stage-7 slices defined by `28_TUTORIAL_ALIGNED_BUSINESS_WORKBENCH_STANDARD.md`:

```text
7.1 Business journey projection
7.2 Demand Radar business surface
```

It deliberately does **not** start Product Definition / Phase B.

The product rule is:

```text
existing durable Opportunity / Analysis / Product truth
-> safe business projection
-> tutorial-aligned Demand Radar
-> human direction decision
```

No fake business opportunity is created for the dashboard.

---

## 2. No generic Project entity was added

The repository already has a real durable relationship:

```text
Analysis
  -> OpportunityRecord
      -> ProductRecord
          -> ContentItem
```

For Stage 7.1/7.2 this is sufficient to answer the operator's current business questions. A new Project/BusinessCase table would add an abstraction without solving a current authority problem.

Therefore V1 reuses existing durable truth and adds a read projection instead of creating Project CRUD.

This follows the Stage-7 standard:

- the operator's first-class object is the demand direction/opportunity;
- the product becomes the business anchor after approval;
- Project/BusinessCase may be added later only if cross-stage relational continuity actually requires it.

---

## 3. Backend business projection

New package:

```text
backend/app/features/business/
  api.py
  schemas.py
  service.py
```

New endpoint:

```http
GET /api/v1/business/demand-radar
```

The endpoint is read-only. It does not change Opportunity, Product, Job, AgentRun, Evidence or Artifact lifecycle.

`BusinessWorkbenchService.demand_radar()` projects existing durable records into operator-facing business state.

### 3.1 Summary projection

The response exposes business counts:

```text
total_direction_count
new_today_count
warming_count
validated_count
pending_decision_count
approved_count
linked_product_count
```

These values are derived from persisted Opportunity/Product state. The frontend does not invent them.

### 3.2 Direction projection

Each direction exposes:

```text
opportunity_id
title
summary
evidence_level
review_status
can_follow_up
journey_stage
is_new_today
supporting_account_count
supporting_product_count
supporting_note_count
image_evidence_count
representative_products
linked_products
next_business_action
created_at
reviewed_at
```

`journey_stage` is a business projection, not a new lifecycle authority:

```text
demand_decision
product_definition
legacy_product_workspace
closed
```

### 3.3 Follow-up authority is backend-derived

The frontend does not decide whether a direction is safe to approve.

`can_follow_up=true` is projected only when the persisted Analysis output contains:

```text
cross_account_conclusion.has_specific_shared_demand == true
```

The existing Opportunity review endpoint remains the write authority.

### 3.4 Existing ProductRecord does not prove the new Product Definition Gate

A historical `ProductRecord` proves only that the pre-Stage-7 product workspace exists.

Therefore an approved Opportunity with existing ProductRecord is projected as:

```text
legacy_product_workspace
```

The UI explicitly says this does **not** mean the new Product Definition Gate has passed.

An approved Opportunity without a ProductRecord is projected as:

```text
product_definition
```

This means "the next business phase is product definition", not "product definition is already approved".

---

## 4. Representative product evidence

The existing Opportunity support projection already stores:

```text
account_user_id
evidence_id
product_id
title
source_url
image_evidence_count
```

The sealed Analysis evidence snapshot stores the trusted shop result used to create the Opportunity.

Demand Radar joins those two immutable/durable views in memory to recover safe representative product facts such as:

```text
price
sold
```

The lookup is based on:

```text
(evidence_id, product_id, source_url)
```

and reads only the already-sealed snapshot. It does not re-open the phone, fetch XHS again, or mutate evidence.

This structure was checked against the production `AnalysisService` snapshot and Opportunity projection implementation, not only against the new test fixture.

---

## 5. Safe media boundary — known V1 gap

The tutorial-standard demand surface expects representative product images to be directly inspectable.

The repository currently has durable image Artifact evidence, but does not yet expose a dedicated browser-safe Artifact image-read boundary suitable for this business surface.

V1 therefore shows:

```text
N 份图片证据
```

plus safe product summary / source URL instead of exposing raw local Artifact paths.

This is intentional.

Forbidden shortcut:

```text
browser <img src="D:\...\artifact.png">
```

or any API that leaks raw local paths.

The next media sub-slice must introduce a contained, identity-checked, read-only media endpoint before the Demand Radar can claim "representative product thumbnails/images are directly visible in the workbench".

Until then, Stage 7.2 is accepted as a business-surface V1 with a recorded visual-evidence display gap; it must not be described as completing the tutorial's full image-inspection requirement.

---

## 6. Frontend routing and mental model

Routes now have distinct business and engineering meanings:

```text
/opportunities
  -> tutorial-aligned Demand Radar business surface

/opportunities/analysis
  -> existing advanced cross-account analysis/operator page

/radar
  -> raw ranking/candidate collection control
```

No old capability was deleted.

Top navigation now calls:

```text
需求雷达 -> /opportunities
采集控制 -> /radar
```

This separates "what business direction should I look at?" from "how is collection being operated?".

---

## 7. Demand Radar UI

The primary page is `frontend/src/pages/DemandRadarPage.tsx`.

It surfaces the tutorial-aligned questions first:

```text
今日新增
正在升温
待你决定
已批准
值得关注的方向
今天需要你关注
```

Each direction can show:

- evidence strength/status badges;
- business journey state;
- supporting account/product/image counts;
- representative products;
- price and visible sales when present in sealed evidence;
- original product link;
- next business action;
- legacy product warning where applicable;
- follow / observe / reject decision controls.

`继续观察` intentionally performs no state mutation in V1. Leaving the Opportunity at `pending_review` is the durable representation of "keep watching" until a later explicit watch lifecycle is justified.

Reject still requires an explicit reason through the existing review write path.

---

## 8. Human Gate A

V1 keeps the existing durable Opportunity review authority and changes only how it is presented.

Business choices are:

```text
跟进这个方向
继续观察
放弃这个方向
```

Rules:

- "跟进" appears only when backend `can_follow_up` is true;
- duplicate review writes are blocked while an action is pending;
- reject requires a reason;
- after review, the page reloads the business projection from backend truth;
- no frontend enum inference grants authority.

---

## 9. Validation completed locally

### Backend

New business tests:

```text
backend/tests/business/test_business_workbench.py
3 passed
```

They prove:

- pending/approved/legacy business stage projection;
- backend-derived `can_follow_up`;
- price/sales recovery from sealed snapshot;
- no raw Artifact path / AgentRun / Job ID leakage in the business API.

Existing compatibility runs:

```text
backend/tests/content
298 passed

backend/tests/agent_runtime
147 passed
```

A broad local Analysis run produced the known Windows evidence-grounding discrepancy:

```text
head: 24 failed, 34 passed in test_evidence_grounding.py
Stage-6 exact base 6bf1f32: same 24 failed, 34 passed
```

Therefore those 24 failures are baseline-equivalent and were not "fixed" by changing unrelated Stage-7 production logic.

### Frontend

Focused Demand Radar + old Opportunities page:

```text
13 passed
```

Full frontend:

```text
82 passed
```

Production build:

```text
PASS
```

Formal Playwright business-surface acceptance:

```text
desktop 1440px: PASS, no horizontal overflow
mobile 390px: PASS, no horizontal overflow
2/2 passed
```

A separate manual browser screenshot inspection was also performed for desktop and mobile. The page visibly prioritizes business summary, directions, evidence and decisions rather than Job/AgentRun details.

### GitHub formal acceptance

Draft PR:

```text
#24
https://github.com/shihaha/xhs-ai-workflow/pull/24
base: spike/physical-collection-boundary-v1
head: spike/project-business-journey-v1
```

Latest code-bearing acceptance before this documentation-only closeout:

```text
Dedicated Stage 7 run: 32690291906 -> SUCCESS
- business projection / Agent authority -> green
- Analysis Stage-6 base/head comparison -> green
- Content Stage-6 base/head comparison -> green
- Demand Radar desktop/mobile -> green

Cumulative Agent Runtime Spike Verification: 32690291902 -> SUCCESS
- baseline/spike frontend -> green
- compile/boundary scan -> green
- controlled E2E regression guard -> green
- baseline/spike full-backend capture -> completed
- Backend regression guard -> green
```

The first dedicated Stage-7 run `32689925105` was not green: its direct Linux Content suite exposed existing platform-specific cleanup failures. That history is preserved. The CI was then corrected to compare Content failures on Stage-6 base and Stage-7 head in the same Linux environment, rather than deleting coverage or changing unrelated production logic. The corrected run `32690291906` passed.

Therefore the accepted wording for this slice is:

```text
本层专用 CI 全绿。
累计回归全绿。
```

This acceptance does not remove the safe-media image-display gap recorded in section 5 and does not authorize entry into Product Definition / Phase B without a separate next-slice decision.

---

## 10. Non-goals

This slice does not add:

- Product Definition lifecycle;
- Product Research Skill;
- product proposals;
- Product Build / UAT;
- content/acquisition Stage D changes;
- generic Project CRUD;
- new physical Android authority;
- new ChatGPT authority;
- AG-UI streaming;
- direct browser access to raw local Artifact paths;
- fake demo opportunities in product runtime.

---

## 11. Next gate

After this branch is formally green in its dedicated CI, stop Stage 7.1/7.2 and review before entering the first B slice.

The next allowed product slice is the one defined in doc 28:

```text
A evidence
-> ChatGPT Product Research Skill
-> 2–3 product proposals
-> Product Definition draft
-> human edit/approve
-> durable approved Product Definition
```

Before that implementation starts, the remaining Demand Radar image-display gap should be explicitly scheduled or accepted as a separate contained-media sub-slice; it must not be silently forgotten.
