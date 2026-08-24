# 28 — Tutorial-Aligned Business Workbench Standard

Status: **Stage 7 pre-implementation product standard**

Date: 2026-08-24

## Why this document exists

Stage 6 proved the bounded Agent / durable Job / HumanAction / Android / Evidence / ChatGPT handoff boundary.

Before Stage 7 adds a business journey layer, the product must be realigned against three sources:

1. the original tutorial article text;
2. the tutorial's actual dashboard / product / content-workflow screenshots;
3. the durable user/AI discussion that mapped the tutorial into A/B/C/D and clarified Skill / Tool / Memory / Harness / Human Gate semantics.

This document is the product-standard gate. Stage 7 implementation must not start from a generic "project-management" abstraction and then force the tutorial into it.

---

# 1. The business journey is A/B/C/D, but the UI is not a four-letter engineering console

The tutorial's source journey is:

```text
find validated demand
-> make a product
-> acquire customers with content
```

Our engineering decomposition remains:

```text
A = Demand / Opportunity
B = Product Research & Definition
C = Product Build & UAT
D = Content & Acquisition
```

The A/B/C/D split is useful because "make a product" contains two different authority questions:

```text
B: What exactly are we going to make?
C: Build the approved thing.
```

Do not expose A/B/C/D as the primary mental model if a normal operator would instead think:

```text
需求雷达
-> 我决定跟进一个方向
-> 把产品想清楚
-> 把产品做出来
-> 围绕产品持续做内容
```

Technical stage names may remain in audit/debug views.

---

# 2. The top-level UX is business-first, not Job-first and not generic Project-first

The tutorial screenshots establish two concrete operator workspaces.

## 2.1 Demand Radar workspace

The demand screen is a business dashboard. Its primary questions are:

```text
今天新发现什么？
什么正在升温？
哪些事情需要我决定？
最值得关注的方向是什么？
支撑这个方向的商品/账号/销量/图片证据是什么？
```

The screenshot pattern includes:

- "今日新增";
- "正在升温";
- "待你决定";
- ranked directions;
- representative product thumbnails;
- short price/sales evidence;
- a prominent "需要你决定" queue;
- detail/evidence on selection.

Therefore Stage 7 must **not** make `/projects` a generic blank CRUD home screen.

A lightweight durable Project/BusinessCase entity may exist internally to connect records, but the operator's first-class object is a **direction/opportunity** and then a **product**.

## 2.2 Product workspace

Once a direction has been approved and becomes a product, the tutorial's product-centric workspace uses the mental model:

```text
产品
关键词
对标
研究
模板
日更
```

This is much closer to the required daily UX than a generic project timeline.

The product is the business anchor. Job / AgentRun / Evidence / Artifact stay authoritative underneath it.

---

# 3. One primary ChatGPT brain, many Skills / logical roles

Do not interpret the tutorial's "main Agent + sub-agents" as a requirement to run seven different foundation models.

The durable discussion standard is:

```text
model = brain
Tool = hand
Memory = durable learned context
Skill = role/SOP
Harness = governed work environment
Workflow = business execution path
Agent = brain + Skills + Tools + state + authority
```

For this project:

- ChatGPT is the primary external reasoning provider;
- Bailian/local models are optional accelerators;
- "产品分析 Agent", "关键词布局 Agent", "单篇拆解 Agent", etc. may initially be distinct Skills / bounded runs using the same reasoning provider;
- they must still have separate result contracts and review boundaries where independence matters.

The main Agent's job is to read durable workbench state, determine what is missing, select the appropriate Skill/tool, and continue until it reaches a Human Gate or a real completion condition.

---

# 4. A — Demand / Opportunity standard

## 4.1 Purpose

A answers:

> What already-validated market demand is worth a human looking at?

It does **not** answer:

> What exact product should we build?

## 4.2 Data source and prioritization

The tutorial uses the Qianfan/XHS ranking signals because click/conversion/transaction evidence is closer to real demand than likes alone.

The ranking/scoring layer only determines **which accounts to inspect first**.

It is not allowed to declare a business opportunity solely from a rank score.

## 4.3 Physical collection boundary

Canonical path:

```text
rank/candidate evidence
-> account
-> real XHS shop on Android
-> product share links
-> computer-side product detail/image acquisition
-> completeness verification
```

Phone and computer have different jobs. The phone handles XHS navigation/share-link discovery; the computer stores/normalizes richer product evidence.

The existing Stage-6 authority boundary remains mandatory:

```text
Agent proposal
-> explicit permission when physical action is required
-> durable child Job
-> physical worker
-> Evidence / Artifact
```

## 4.4 Completeness is a gate, not a nice-to-have

The tutorial standard is N/N:

```text
number of unique product links discovered
== number of complete local product detail records
```

If they do not match, the account does not enter final analysis.

Stage-6 `shop.preflight` is only a cheap representative gate. It is not a substitute for the later full evidence-completeness contract.

## 4.5 Images are first-class evidence

This is non-negotiable.

For opportunity analysis:

- every relevant product image must actually be opened/inspected;
- title-only reasoning is insufficient;
- image-derived conclusions must point back to account + product + exact image/artifact;
- an account with unread product images cannot enter final cross-account aggregation.

Reason: virtual products often hide the meaningful information in images:

- package contents;
- result examples;
- usage flow;
- different pricing tiers;
- templates/modules;
- visual proof of the deliverable.

## 4.6 Per-account analysis

After complete evidence exists, ChatGPT/Agent should derive at least:

- what category/form is being sold;
- price band;
- highest visible sales;
- follower scale;
- low-follower/high-sales signal;
- what the buyer receives;
- delivery complexity;
- product-image observations;
- obvious copyright/platform/delivery risk.

## 4.7 Cross-account aggregation

A single seller only proves that one seller exists.

A direction becomes stronger when multiple independent accounts repeatedly sell similar product forms.

Aggregation output should include:

- supporting account count;
- supporting product count;
- price range;
- highest visible sales;
- recurring product form / use case;
- representative product-image evidence;
- market-status conclusion;
- risk notes;
- recommended next action.

## 4.8 Human Gate A

The tutorial's operator model is "AI automatically runs; human does the choice question."

The Demand Radar should therefore end with a prominent decision, not an internal lifecycle enum:

```text
跟进这个方向
继续观察
放弃/排除
需要更多证据
```

Only an approved direction may enter B.

---

# 5. B — Product Research & Definition standard

## 5.1 Purpose

B answers:

> Exactly what should we make, for whom, in what form, and why this version?

A validated opportunity is **not** sufficient authority to start building.

## 5.2 Reuse A evidence

When a human selects a direction, B begins from the evidence already collected in A:

- seller/product records;
- prices;
- main/detail images;
- sales evidence;
- account analysis;
- cross-account opportunity evidence.

Do not recollect the same phone evidence merely because a new stage started.

## 5.3 What AI should research

ChatGPT/Product Skill should help answer:

- who is the target user;
- what core problem is being solved;
- major usage scenarios;
- what current sellers offer;
- how current sellers present it;
- delivery form;
- required product modules/features;
- pricing evidence;
- gaps and differentiation options;
- risks/constraints;
- 2–3 viable product concepts.

For virtual资料 products, purchased comparison materials may be useful. Payment itself is a human action; AI can analyze the materials after they are supplied.

Purchased materials are research references, not copy sources. The resulting product still needs an independently organized/original structure.

## 5.4 Product Definition is the real contract

A Product Definition should contain at minimum:

```text
one-line product definition
目标用户
核心问题
主要使用场景
产品形态
核心交付物 / 功能
关键页面或模块
差异化
价格/价格区间假设
证据引用
不做什么
验收标准
```

The product workspace's "产品" tab should eventually present the human-readable version of this contract, similar to the tutorial screenshot's product overview / target audience / core problems.

## 5.5 Human Gate B

The human decides/approves:

- whether to make this direction at all;
- product form (资料 / website / mini-program / local tool / etc.);
- differentiation;
- pricing direction;
- structure / key feature set;
- Product Definition.

ChatGPT should propose and compare. It must not silently approve its own Product Definition.

Only an approved Product Definition may enter C.

---

# 6. C — Product Build & UAT standard

## 6.1 Virtual资料 products

Typical path:

```text
approved Product Definition
-> AI drafts structure
-> create modules/content
-> format/package
-> completeness check
-> factual/quality check
-> deliverable
-> human UAT
```

## 6.2 Website / app / mini-program / local-tool products

The tutorial imposes an unusually clear visual gate. Preserve it.

### Gate C1 — high-fidelity image prototype only

First:

```text
read approved product requirements
-> generate high-fidelity UI IMAGE prototypes
-> one image per core page / important state
-> realistic content
-> show layout / color / typography / spacing / buttons / hierarchy
```

At this stage:

```text
NO UI code
NO business logic
```

The human opens every prototype image and gives visual/product feedback.

**Prototype not satisfactory = do not continue.**

### Gate C2 — 1:1 UI implementation

Only after all prototype images are approved:

```text
implement UI to match approved images
-> run each real page
-> screenshot it
-> compare side-by-side with prototype
-> fix layout/text/color/font/spacing/image differences
```

Do not mix business logic into the visual-reproduction phase.

### Gate C3 — business logic

After visual parity:

- button behavior;
- data source/state;
- submit/result behavior;
- routing/navigation;
- error/empty/loading states;
- persistence/integration.

### Gate C4 — stepwise real UAT

The tutorial's acceptance pattern is not "tests passed, ship it."

For a non-programmer, the final questions are:

```text
Does the real page match the approved prototype?
Does the product function correctly when used like a real user?
```

The test Agent should operate step by step:

```text
open -> screenshot/check
click -> screenshot/check
input/submit -> screenshot/check
next state -> screenshot/check
```

A failing step stops and is repaired before continuing.

## 6.3 Human Gate C

Human UAT confirms both:

1. visual/product intent matches the approved prototype;
2. real functionality works.

Only then is the product allowed to enter D.

---

# 7. D — Content & Acquisition standard

D is not "ask AI to write posts."

Canonical chain:

```text
产品资料
-> 产品分析
-> 关键词布局
-> 手机搜索/采短链
-> 电脑入库
-> 单篇拆解
-> 模板聚类
-> 人 + AI 固化成 Skill
-> 日更生成
-> 独立内容审查
-> optional human review
```

## 7.1 Product tab — factual source of truth

The Product Analysis Skill keeps raw product files and creates a human-readable product overview/index.

Expected truth set includes:

- price;
- functions;
- audience/scope;
- usage/results;
- purchase method;
- cases/reviews;
- screenshots/real interface where relevant.

This is a factual gate. If the system cannot clearly say what the product is, who it is for and what problem it solves, content generation must not continue.

## 7.2 Keyword tab — searchable demand angles, not final hashtags

Keywords should cover more than product name:

- product/function;
- target audience;
- scenario;
- problem/pain point.

The tutorial UI allows per-keyword configuration such as:

- actual-note target count;
- engagement threshold;
- A–Z expansion;
- whether comments are collected;
- recollect/retry state.

The first run should be deliberately small before scaling.

## 7.3 Benchmark collection — validate before batch

Tutorial standard: test a small sample first (e.g. two notes), then verify:

For image/text posts:

- title/body complete;
- first image is the cover;
- inner-image order is correct;
- engagement data is present.

For video:

- original video opens;
- cover exists;
- key frames exist;
- transcript exists.

Only then batch collect.

Restart/resume must preserve completed items and continue instead of duplicating the batch.

## 7.4 Benchmark detail must be visually inspectable

The tutorial's benchmark screens show the note itself, media,正文/话题, metrics and original link.

Do not reduce the operator view to a table of IDs.

## 7.5 Research / decomposition

Each note is decomposed into durable research results.

Clustering operates on prior decomposition results instead of repeatedly rereading every raw note.

Visually similar content with different content intent should not be merged merely by appearance.

## 7.6 Template evidence threshold

A recurring pattern requires multiple supporting notes.

Tutorial standard:

```text
at least 3 supporting benchmarks for one class
```

With only one or two examples, collect more instead of turning coincidence into a permanent rule.

## 7.7 Human + AI convert template draft into executable Skill

A template draft is research, not yet production authority.

The final content Skill must specify concrete execution rules, including:

- exact allowed product source files;
- title formula;
- body structure;
- purpose of each section;
- topic strategy;
- interaction point;
- prohibited claims;
- cover composition;
- role of every inner image;
- which images are product screenshots vs redesigned images;
- image Skill/script to call;
- output format/order.

Generic rules such as "make the title attractive" are not a usable Skill.

## 7.8 Complete content package, not a text draft

Daily generation output is one complete package:

```text
标题
正文
话题
封面
全部内页图
```

Each image must actually be opened and checked.

A new Skill should first generate one complete package. Repeated defects change the Skill; one-off defects change only the current content item.

## 7.9 Separate generation and review

The generating run must not simply mark its own content approved.

A separate review run rereads the same product truth + Skill and checks:

- factual accuracy;
- title/body compliance;
- image text readability;
- arrows/callouts point to the right place;
- all required assets exist;
- package completeness.

Failed review returns to generation/editing and is reviewed again.

## 7.10 Daily continuation

The main Agent reads durable progress first:

```text
daily target 3
already completed 1
=> generate 2, not 3 more
```

If the previous run stopped at generation/review/rework, resume there. Do not regenerate a fresh batch.

The tutorial's 日更 screen is therefore a quota/progress/calendar view, not only a "Generate" button.

---

# 8. UI standard derived from the tutorial screenshots

The screenshots are reference for **information architecture and operator clarity**, not a requirement to clone Obsidian/CSS pixel-for-pixel.

## 8.1 Demand Radar

Must make visible without technical knowledge:

```text
今天发现多少
哪些升温
哪些需要我决定
Top directions
representative product images
price/sales signal
why this direction exists
```

Human decisions must be visually prominent.

## 8.2 Product workspace

The core navigation model should converge toward:

```text
产品 | 关键词 | 对标 | 研究 | 模板 | 日更
```

Optional technical/audit drawers can expose Job / AgentRun / Evidence / HumanAction.

Do not invert this hierarchy.

## 8.3 Evidence should be inspectable in context

The tutorial repeatedly lets the operator click from summary to the actual product/note view.

Therefore evidence UX should preserve:

```text
business conclusion
<-> exact account/product/note
<-> exact image/media/artifact
```

## 8.4 Status language

Prefer simple Chinese business states such as:

```text
新增
升温
待你决定
待分析
已拆解
草案 / 待固化
已固化 / 可日更
生成中
审查中
返工
待人工审核
完成
```

Backend enum values remain available only for audit/debug.

## 8.5 Right-side "today / needs attention" rail

The screenshots repeatedly surface current work and exceptions in a visible side rail.

Stage 7 should preserve the idea:

```text
今天要做什么
哪里失败
哪里需要人决定
哪里在等待 ChatGPT
```

This is more useful than a generic notification center.

---

# 9. Project entity: allowed internally, forbidden as an empty abstraction

Stage 7 may add a lightweight durable Project/BusinessCase entity **only if it solves relational continuity** across A/B/C/D.

It may reference:

- Opportunity/Direction;
- approved Product Definition;
- Product/Build/UAT;
- content workspace;
- Jobs/AgentRuns;
- Evidence/Artifacts;
- Human Gates.

It must not:

- copy authoritative Job lifecycle state;
- copy Evidence/Artifact content;
- invent completion from UI state;
- become the primary blank screen users manage by hand;
- force every technical Job to be manually attached by the operator.

The UI should feel like:

```text
I found a direction
-> I approved a product
-> the product is being built
-> the product is now acquiring traffic
```

not:

```text
I created Project #17 and attached Job #83.
```

---

# 10. Human Gate standard

The tutorial and the durable architecture agree on one core principle:

> AI can execute extensively, but cannot be the sole authority that declares its own important proposal correct.

Required business gates:

```text
Gate A: choose / reject / observe opportunity
Gate B: approve Product Definition
Gate C1: approve every key image prototype
Gate C4: approve real product UAT
Gate D1: approve product-truth summary when first established/changed
Gate D2: co-approve/finalize a new content Skill
Gate D3: optional human content review, especially while a Skill is new
```

Physical/external side-effect permissions remain separate security gates underneath these business gates.

---

# 11. Stage 7 implementation order after this standard

Do **not** implement all A/B/C/D at once.

The next safe slice should be:

## 7.1 Business journey projection

Add only enough durable linking/projection to answer:

```text
What direction/product am I looking at?
What phase is it in?
What has been completed from durable truth?
What is the next business action?
What needs my decision?
What evidence supports it?
```

## 7.2 Demand Radar business surface

Project the already-existing A evidence/opportunity work into the tutorial-aligned demand screen.

Do not create fake demo business truth.

## 7.3 Approved direction -> Product Definition workspace

Only after 7.1/7.2 are proven, add the first B slice.

The first B acceptance is **not** product building. It is:

```text
A evidence
-> ChatGPT Product Research Skill
-> 2–3 product proposals
-> Product Definition draft
-> human edit/approve
-> durable approved Product Definition
```

Then stop and review before C.

---

# 12. Acceptance rule for every future slice

A slice is not "tutorial aligned" merely because the backend endpoint exists.

Each slice must be checked against all four layers:

```text
Business meaning
Durable authority/state
AI/Skill behavior
Operator-visible evidence/UI
```

And when source images are part of the tutorial requirement, acceptance must include actually opening/inspecting the image output rather than trusting filenames, metadata or OCR summaries.
