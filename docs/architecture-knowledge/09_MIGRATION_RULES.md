# 09 — Migration Rules for XHS Workbench Next

- Initial version: 2026-08-23
- Status: binding research guardrails until superseded by an explicit ADR.

## 1. Treat the old system as a business-truth oracle, not a code template

The current repository contains proven semantics learned through real UAT. A new implementation does not have to preserve current file structure, UI or service class layout, but it must preserve the relevant business outcomes and failure semantics.

The target is:

> semantic parity first, architectural improvement second.

## 2. Never rewrite historical evidence to make migration easier

Do not mutate old analyses, Opportunity evidence, artifacts, hashes or review facts to fit a new schema.

If the next-generation system needs a new representation, create an explicit migrated/reference record pointing back to the original source identity.

Historical but ineligible evidence stays historical and ineligible.

## 3. Keep runtime facts outside Git

Do not migrate live SQLite databases, cookies, browser profiles, API keys, phone evidence or large runtime artifact stores into the source repository.

Git should contain:

- code;
- schemas/specifications;
- architecture knowledge;
- safe fixtures;
- migrations;
- non-secret source/reference material where rights permit.

Runtime keeps business records and evidence bytes.

## 4. Preserve durable Job semantics

Any new runtime must preserve at least:

- queued/running/needs_human/succeeded/failed/cancelled distinction;
- durable progress and error category;
- explicit lease/recovery behavior;
- no silent success after process interruption;
- explicit human restart for unsafe physical work;
- idempotent artifact/result binding where replay is possible.

An Agent runtime may add `AgentRun` / `AgentStep`; it must not weaken the outer durable Job lifecycle.

## 5. Preserve evidence lineage

Every business claim that currently requires evidence must remain traceable to durable source facts.

Future compact context/summaries must carry source evidence references. A model-generated summary is not a new trusted source merely because it is shorter or easier to retrieve.

Context compaction may reduce tokens; it may never upgrade evidence trust.

## 6. Preserve adapter normalization boundaries

External page/API/device shapes belong behind adapters.

Future business Tools should generally wrap safe domain operations, for example:

```text
Tool: account.read_latest_notes
  → XhsCollectionService / adapter
  → normalized records/evidence
```

Do not expose arbitrary selectors, shell commands, browser JavaScript or unvalidated provider payloads as normal business-agent inputs.

## 7. Add an explicit Product Definition gate

The next-generation business model must distinguish:

```text
Opportunity approved for research
≠ Product Definition approved for build
```

A concrete Product Definition must be durable and separately human-approved before a build run starts.

The Agent can research and propose definitions. It cannot approve its own definition.

## 8. Require a Finished Product gate before content

Content research/production may start only after the real product has:

- concrete version/identity;
- actual deliverables/features;
- real screenshots/materials where relevant;
- usage instructions/known facts;
- claims boundaries;
- human UAT status.

These facts should form a durable `Finished Product Dossier` or equivalent.

Do not let a content Agent infer a product from an Opportunity title or unfinished prototype.

## 9. Keep publishing outside automatic V1 execution

Current system does not auto-publish, like, favorite or comment on Xiaohongshu.

Open-source workbench/agent projects that support arbitrary browser automation must have those actions denied by our permission layer unless a future explicit business decision changes this rule.

## 10. Fail closed when prerequisites disappear

If a required model key, browser profile, authenticated session, Android device, layout contract, evidence file or trusted DB record is unavailable:

- mark the relevant Tool unavailable; or
- produce a durable failed/needs-human outcome.

Do not replace missing real dependencies with demo records or model guesses.

## 11. Migrate one vertical slice at a time

Recommended migration method:

```text
old system fixed input/evidence
          │
          ├────────→ old result
          │
          └────────→ new vertical slice result
                          │
                          ▼
              compare business result,
              evidence coverage, failures,
              token/cost, state/recovery
```

Only expand after a slice passes parity and new-runtime acceptance tests.

The current `七宗罪` Phase A case should be one fixed replay/reference case, together with synthetic edge fixtures for failure paths.

## 12. Keep the old system runnable until parity is demonstrated

Do not make the old baseline unusable merely to force migration.

A new repository/branch should be able to fail as an experiment without destroying the current business truth system.

## 13. Open-source components must conform to our contracts

Selection priority is not “most stars” or “prettiest UI.”

A candidate must be evaluated on whether it can host or integrate with:

- durable jobs;
- evidence lineage;
- human approvals;
- needs-human states;
- bounded tools and permissions;
- local Windows execution;
- our Python business layer or a clean service boundary;
- token/context observability;
- resumable runs;
- explicit licensing compatible with intended use.

If a framework requires us to weaken those constraints, reject the framework rather than weakening the business rules.

## 14. Do not vendor reconstructed proprietary Claude Code source

Recovered/reconstructed Claude Code 2.1.88 materials may be studied for architectural concepts such as:

- tool contracts;
- permissions;
- bounded agent loops;
- context management;
- checkpoints;
- specialized agents;
- hooks and observability.

They must not be copied or vendored into the product without a valid license/legal basis. The current recovered repositories audited so far do not provide a clean open-source license for the restored Anthropic source.

Implement useful concepts independently or use genuinely licensed open-source alternatives.

## 15. Every external dependency requires license and maintenance review

Before adopting an open-source workbench/runtime/backend:

1. identify exact repository and version/commit;
2. verify LICENSE/SPDX and relevant dependency licenses;
3. check project activity and maintainability;
4. identify what code we would actually depend on;
5. estimate integration/removal cost;
6. record accept/reject rationale in `docs/open-source-evaluation/` and, for architecture-changing choices, an ADR.

No license = no production-base recommendation merely because the code is public.

## 16. Do not start with multi-agent swarm architecture

First prove:

- one bounded Agent Runtime;
- Tool Registry;
- central PermissionPolicy;
- Context Builder;
- checkpoint/resume;
- AgentRun/AgentStep observability;
- one real vertical slice.

Specialized subagents can be added after that. Swarm/team features are optional, not a prerequisite for a useful workbench.

## 17. Preserve human agency at irreversible business transitions

At minimum, current next-generation design should keep explicit human decisions for:

- Opportunity disposition;
- Product Definition approval;
- final Finished Product UAT;
- new production template/Skill approval where the content system requires it;
- actual publication/high-risk external state changes.

The workbench should make these decisions easy, visible and durable instead of hiding them in chat.

## 18. Migration acceptance principle

A rewrite is not an improvement merely because its code is cleaner.

For every migrated slice, ask:

1. Did it reach the same or more correct business result?
2. Is every important conclusion still evidence-traceable?
3. Does interruption still fail safely?
4. Can an operator understand what happened?
5. Are permissions stricter or equal?
6. Is context/token usage controlled?
7. Is the implementation easier to extend without duplicating state ownership?

If the answer is worse on the first five, the migration does not pass.
