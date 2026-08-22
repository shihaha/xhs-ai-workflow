# 01 — Tutorial Business Model

- Audit date: 2026-08-23
- Purpose: preserve what the original tutorial actually describes, separated from our current implementation choices.

## 1. The tutorial's operating model

The tutorial is not fundamentally about a dashboard or a single monolithic application. It describes an AI-assisted business operating system for Xiaohongshu virtual products.

Its core division of labor is explicit:

- AI handles repetitive collection, organization, analysis and execution.
- Human handles choosing directions, defining products and final acceptance.

The original article describes three major stages:

```text
1. Find demand
   ↓
2. Make product
   ↓
3. Acquire traffic/content
```

The three stages are connected, but they are not the same problem and do not need to be implemented by one giant autonomous Agent.

## 2. Stage A — Find demand

The tutorial's demand loop is evidence-first:

1. scan Xiaohongshu Qianfan's eight entrances: four boards × excellent content/account views;
2. score accounts using commercial signals, with GMV/payment conversion more important than raw reads;
3. prioritize accounts that repeatedly appear, span boards, or show sales despite low follower counts;
4. let AI operate the phone to inspect each account shop and obtain product share links;
5. use the computer to collect product details and images;
6. verify collection completeness before analysis;
7. analyze each account's category, pricing, top products, follower level and low-fan/high-sales characteristics;
8. compare multiple accounts and identify market directions supported by real products, prices, visible sales and image/detail evidence;
9. present the result to the human, who chooses whether to continue and what direction deserves product research.

The account score is therefore a prioritization mechanism, not the final business decision.

## 3. Demand-radar shared package

The shared demand-radar package makes the above stage more concrete. Its main chain is approximately:

```text
Qianfan 8 boards
→ raw facts / SQLite
→ account scoring
→ phone product collection
→ detail + image evidence
→ N/N verification
→ per-account image/text analysis
→ cross-account aggregation
→ demand cards / Obsidian dashboard
→ human decision
```

Important original design ideas:

- evidence must be complete before an account enters opportunity judgment;
- image evidence is actually inspected rather than inferred from titles;
- per-account subagents do not perform cross-account conclusions;
- cross-account aggregation happens after per-account evidence is prepared;
- human decides whether a direction advances.

The shared package contains empty product-experiment directories, but it does not contain a complete generalized Product Research Agent, Product Build Agent and production-grade state machine. It should not be misread as a complete end-to-end product manufacturing backend.

## 4. Stage B/C — Make product

The tutorial's wording is important: **human defines the direction, AI executes**.

Once a direction is selected, the existing evidence already answers part of the market question: what products sell, at what visible prices, and what their pages/materials look like. The next work is not simply “generate something with AI.” It is to:

- analyze relevant products/benchmarks;
- understand the target user and purchase motivation;
- determine a concrete product form;
- design its structure/content/functions;
- then execute production.

The production method branches by product type:

- virtual materials/documents: define structure, produce sections, inspect final deliverables;
- website/miniprogram/local software: create visual prototypes, build the real interface and functions, then perform real user-path acceptance;
- other forms: choose an appropriate production workflow.

For a non-programmer, the tutorial's acceptance principle is practical: the real page should match the approved prototype and the intended functions should work. Only then is the product finished.

This confirms two different gates:

```text
Opportunity
→ decide the concrete Product Definition
→ build it
→ accept the real Finished Product
```

A market Opportunity is not itself a product specification.

## 5. Stage D — Content acquisition

The content system starts **after a real product exists**.

The article's content chain is:

```text
Finished product / complete product materials
→ product analysis
→ keyword layout
→ benchmark collection
→ single-note breakdown
→ template clustering
→ human + AI template workshop
→ production SKILL
→ daily content generation
→ independent content/visual review
→ human review / own publishing method
```

The normal operator communicates mainly with a main Agent. The main Agent reads the workbench, judges current progress and delegates to specialized roles such as:

- product-analysis agent;
- keyword-layout agent;
- single-note analysis agent;
- template-clustering agent;
- daily-generation agent;
- content-review agent.

Results are written back to the workbench/Obsidian instead of living only in chat.

## 6. Content-system shared package

The shared content package strongly reinforces the precondition: it expects complete product materials under a stable product directory.

Its product-analysis agent is not a product-creation agent. It reorganizes facts about an existing product for content strategy. Missing facts are marked as missing, inference is labeled, and it must not invent product claims.

Other useful rules:

- keyword research is derived from real product facts;
- phone collection and desktop parsing are separated;
- benchmark notes are deduplicated across keywords;
- single-note analysis handles one note at a time;
- template clustering works on structured analyses rather than mixing all raw notes;
- a recurring pattern needs multiple supporting notes before becoming a template;
- `SKILL.md` is the executable production specification and must cite actual product files;
- daily production is serial: generate → review → revise/pass;
- visual review must actually open every image; inability to inspect an image is a blocker, not a pass;
- the shared flow does not auto-publish to Xiaohongshu.

## 7. Human/AI responsibility boundary

A concise reconstruction of the tutorial's intended split is:

| Stage | AI should do | Human should own |
|---|---|---|
| Demand | collect, normalize, score, inspect, compare, summarize | decide whether a direction deserves further work |
| Product research | research benchmarks, organize evidence, propose concrete definitions | choose/approve the actual Product Definition |
| Product build | execute document/software production, tests and revisions | approve critical visual/function decisions and final UAT |
| Content | research benchmarks, derive templates, generate/review packages | approve new template rules and final publishing decisions |

The tutorial is therefore not evidence for “fully autonomous AI runs the business with no human gates.” Its own workflow keeps humans at direction, product-definition and acceptance decisions.

## 8. What the tutorial does not give us

The tutorial and shared packages do not provide a ready-made, production-grade implementation of all of the following in one backend:

- a universal Agent Runtime;
- a unified Project/Run/Step state machine across A/B/C/D;
- a generalized Product Definition entity and approval workflow;
- a generalized Product Builder for every product type;
- a single durable permission system for all risky actions;
- a commercial workbench shell suitable for our exact architecture;
- a complete context/memory strategy for very large evidence sets.

Those are engineering problems we still need to design or select from licensed open-source projects.

## 9. Implications for XHS Workbench Next

The next-generation system should preserve the tutorial's **business semantics**, not its exact UI or folder structure.

Minimum end-to-end business graph:

```text
Demand Evidence
→ Opportunity
→ Human-approved Product Definition
→ Product Build Run
→ Human-approved Finished Product
→ Finished Product Dossier
→ Content Research
→ Approved Template / Skill
→ Content Production
→ Content Review
→ Pending-publication package
```

Agent autonomy may increase inside each bounded segment, but it must not erase the human transition gates between them.

## 10. Source hierarchy

Primary tutorial source:

- `references/original-tutorial/406亿Token教程/原文逐字可读版.md`

Shared implementation digests:

- `references/original-tutorial/需求雷达-分享版/REFERENCE_DIGEST.md`
- `references/original-tutorial/内容系统-分享版/REFERENCE_DIGEST.md`

For current-system behavior, these sources are subordinate to current code, current UAT and current handoff documents.
