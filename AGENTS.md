# Repository instructions for Codex / AI agents

Before making changes, read these files in order:

1. `docs/START_HERE.md`
2. `docs/AI_HANDOFF_CURRENT.md`
3. `docs/CODEX_NEXT_OBJECTIVE.md`
4. `docs/REFERENCE_MAP.md`

Then read only the code/docs needed for the current task. Do not begin by scanning the entire repository or all tutorial references.

## Current architecture boundary

The business flow is intentionally separated into:

- A — demand radar / Opportunity discovery: current repository core.
- B — product research / product definition: currently an independent human+AI work segment.
- C — product build: independent AI/Codex build segment after a concrete product definition is approved.
- D — content system: only after a real Finished Product exists and passes human UAT.

Do not automatically jump from an Opportunity to product creation or content generation.

## Current case

The current Opportunity is `七宗罪心理测试数字内容市场机会`.

The user has approved continuing this Opportunity into product research. This approval means “continue researching the opportunity”; it does not define the final product form and does not itself start product build or content production.

If the local persisted Opportunity is still `pending_review`, use the existing human-review path to persist the approval rather than rewriting historical evidence or analysis records.

## Reference material

Original tutorial/reference material is indexed under:

- `references/original-tutorial/406亿Token教程/教程正文整理版.md`
- `references/original-tutorial/需求雷达-分享版/REFERENCE_DIGEST.md`
- `references/original-tutorial/内容系统-分享版/REFERENCE_DIGEST.md`
- `references/original-tutorial/SOURCE_INVENTORY.md`

These files explain the original author's method and shared reference implementations. They are not authoritative over current production code.

Priority when facts conflict:

`current handoff / current code / current UAT` > `tutorial text` > `shared reference implementation`.

Preserve historical evidence and fail closed when current evidence/trust requirements are not met.
