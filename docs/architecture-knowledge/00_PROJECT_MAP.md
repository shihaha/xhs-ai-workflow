# 00 — Project Map

- Status: first deep internal audit completed
- Audit date: 2026-08-23
- Research branch: `research/xhs-workbench-next`
- Baseline branch: `feature/system-v1`

## 1. What this repository actually is

`xhs-ai-workflow` is a Windows-local, evidence-backed Xiaohongshu business workbench implemented as a **modular FastAPI monolith + SQLite + React operator UI**.

It is not an Agent Runtime today. The core services execute explicit, deterministic business flows and call models as bounded structured functions. The model does not own a general tool loop, central permission policy, resumable agent-step ledger, or context planner.

The current business architecture intentionally separates four segments:

A. Demand radar / Opportunity discovery.
B. Product research / product definition.
C. Product build after an explicitly approved product definition.
D. Content system only after a real Finished Product exists and passes human UAT.

The active, proven business core is A. B and C are intentionally independent human+AI work segments until enough real cases prove which steps should be generalized. D has substantial code already present in this repository, but business authorization and code existence are different concepts: the current handoff still forbids treating that code as permission to skip B/C or to start content production before a Finished Product exists.

## 2. Current real business state

Current real Opportunity:

- title: `七宗罪心理测试数字内容市场机会`
- Opportunity ID: `caed5776-ef38-4a0a-90fe-57ae2291583e`
- analysis ID: `5859e6c9-0485-451b-9152-05896c0d037b`
- evidence level: `warming_candidate`
- persisted review state: `approved`

The approval means: **continue this Opportunity into independent product research**. It does not approve a concrete Product Definition, does not authorize product build, and does not authorize content production.

The Phase A ranked pool reached 71 candidates: 2 `in_scope`, 31 `out_of_scope_physical`, 36 `needs_human`, 2 `collection_failed`, 0 waiting. The successful current analysis used the two currently eligible accounts and their trusted evidence.

## 3. Current system shape

```text
React operator UI
  /radar /accounts/:id /opportunities /content /status /jobs
                    │
                    ▼
FastAPI composition root
                    │
       ┌────────────┼─────────────┐
       ▼            ▼             ▼
     Radar        Analysis      Content/Media
       │            │             │
       ├───────┬────┴──────┬──────┤
       ▼       ▼           ▼      ▼
    Adapters  Jobs       Evidence  Product/content artifacts
       │       │           │
       └───────┴─────┬─────┘
                     ▼
                  SQLite
                     │
                     ▼
      runtime files / manifests / SHA-256
```

External operations are isolated behind adapters. Runtime bytes and the business database live outside Git under `D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench`.

## 4. Strong assets already present

The following are not prototypes and should be treated as migration assets unless later evidence disproves them:

- durable `JobService` state machine with leases, compare-and-set transitions, logs and artifact bindings;
- explicit `needs_human` recovery after interrupted work, especially physical Android/browser work;
- strict adapter contracts and a capability-based adapter registry;
- real Qianfan scoring and candidate funnel logic;
- XHS account/profile/latest-note evidence collection;
- Android shop preflight, discovery and bounded evidence sampling;
- artifact/source/manifest/SHA-256 evidence chain;
- strict Pydantic analysis schemas and evidence grounding;
- before/after-model evidence trust revalidation;
- immutable analysis evidence snapshots and one-way Opportunity review gate;
- real UAT history covering phone, browser, selector, provider, filesystem and database failure modes;
- content/material/revision/package infrastructure that may be selectively reusable after its business role is revalidated.

## 5. Important distinction: code exists vs business stage is active

The repository already contains `content` and `media` features and a `/content` page. It can create Product records, manage materials, generate/review content revisions and export deterministic packages.

That does **not** mean the current architecture has a complete tutorial-equivalent Phase B product-research system or Phase C product-building system, and it does not override the current human gates.

This distinction must remain explicit in all next-generation design work:

- **implemented code surface** = technical capability exists;
- **proven business capability** = real flow has passed the required acceptance/UAT;
- **authorized current phase** = the user has explicitly allowed that business transition.

These are three different facts.

## 6. Authority order

When facts conflict:

`current handoff / current code / current UAT` > `tutorial text` > `shared reference implementation`.

The tutorial explains the original author's method. The current repository and real UAT determine what our current system actually does.

## 7. Knowledge entry points

Future research sessions should begin with:

1. `docs/START_HERE.md`
2. `docs/AI_HANDOFF_CURRENT.md`
3. `docs/CODEX_NEXT_OBJECTIVE.md`
4. `docs/REFERENCE_MAP.md`
5. `docs/WORKBENCH_RESEARCH_INDEX.md`
6. this `docs/architecture-knowledge/` directory

## 8. Reference material status

Already indexed:

- `references/original-tutorial/406亿Token教程/原文逐字可读版.md`
- `references/original-tutorial/406亿Token教程/教程正文整理版.md`
- `references/original-tutorial/需求雷达-分享版/REFERENCE_DIGEST.md`
- `references/original-tutorial/内容系统-分享版/REFERENCE_DIGEST.md`
- `references/original-tutorial/SOURCE_INVENTORY.md`

The readable tutorial body is in Git. The original large SingleFile HTML itself and the full raw extracted contents of the two shared ZIPs are not yet fully archived in Git; current digests must not be mistaken for complete raw source archives.

## 9. Remaining research before architecture selection

The first deep internal audit is complete enough to freeze the current-system model. Remaining work before selecting a new implementation base:

1. deep architectural study of Claude Code concepts and other mature agent runtimes;
2. derive Agent Runtime, Workbench UI and backend requirements from this project's actual constraints;
3. search licensed open-source candidates;
4. score candidates against requirements, integration cost and licenses;
5. record ADRs;
6. decide whether to evolve this repository or create `xhs-workbench-next`.

Do not begin a wholesale rewrite before those steps are complete.
