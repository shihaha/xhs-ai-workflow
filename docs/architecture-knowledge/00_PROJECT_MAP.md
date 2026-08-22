# 00 — Project Map

- Status: initial baseline; deep audit not yet complete
- Research branch: `research/xhs-workbench-next`
- Baseline branch: `feature/system-v1`

## What this repository currently represents

The repository is an evidence-backed Xiaohongshu workflow/workbench. Its current architecture intentionally separates the business into four segments:

A. Demand radar / Opportunity discovery.
B. Product research / product definition.
C. Product build after an explicitly approved product definition.
D. Content system only after a real Finished Product exists and passes human UAT.

The current repository core is Phase A. The boundaries between A/B/C/D are intentional and must not be silently collapsed during next-generation workbench research.

## Current real case

The current real Opportunity is `七宗罪心理测试数字内容市场机会`.

Existing project instructions state that Phase A has formed a two-account evidence loop and a successful cross-account analysis. The user has approved continuing this Opportunity into product research, but that does not itself approve a concrete product definition, start product build, or start content production.

If persisted state and human approval differ, the existing human-review path must be used rather than rewriting historical evidence or analysis.

## Current authority order

When facts conflict:

`current handoff / current code / current UAT` > `tutorial text` > `shared reference implementation`.

The tutorial is used to understand the original author's method. The current repository and real UAT determine how our system actually works.

## Existing project entry points

Future research sessions should begin with:

1. `docs/START_HERE.md`
2. `docs/AI_HANDOFF_CURRENT.md`
3. `docs/CODEX_NEXT_OBJECTIVE.md`
4. `docs/REFERENCE_MAP.md`
5. `docs/WORKBENCH_RESEARCH_INDEX.md` when working on next-generation workbench research

## Original reference material already indexed in the repository

- `references/original-tutorial/406亿Token教程/教程正文整理版.md`
- `references/original-tutorial/需求雷达-分享版/REFERENCE_DIGEST.md`
- `references/original-tutorial/内容系统-分享版/REFERENCE_DIGEST.md`
- `references/original-tutorial/SOURCE_INVENTORY.md`

## What is not yet claimed by this document

This initial map does not yet claim a complete understanding of:

- every backend module and database table;
- every frontend route and state owner;
- all job/evidence invariants;
- all UAT failures and recovery behavior;
- all tutorial requirements;
- the correct next-generation workbench architecture;
- the best open-source Agent Runtime, UI, or backend candidate.

Those require the planned deep audit. Findings must be written back into this knowledge base rather than left only in chat memory.
