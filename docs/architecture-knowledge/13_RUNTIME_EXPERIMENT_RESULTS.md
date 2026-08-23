# Runtime and Context Experiment Results

Last updated: 2026-08-23

This note is the durable research-branch record for implementation experiments that were intentionally developed on isolated branches. It distinguishes validated behavior from hypotheses and currently blocked work.

## 1. Stage 2 Agent Runtime

Branch: `spike/agent-runtime-pydantic-v1`
Validated head: `e74181c2eadd485fd6a70d00f02a4bb7fb8b517f`

### Validated behavior

Focused Agent Runtime acceptance passed **23/23**.

The accepted runtime semantics include:

- durable `AgentRun` / step persistence;
- one structured model action per bounded turn;
- centralized tool schema validation and permission policy;
- bounded model calls, steps, tokens and wall time;
- explicit checkpointing and recovery;
- no hidden chain-of-thought persistence;
- explicit resume after interruption;
- no automatic replay of ambiguous in-flight actions;
- idempotent committed-result reuse only when tool identity and arguments match;
- non-idempotent duplicate call blocking;
- possible side-effect timeout/exception -> `needs_human` with `uncertain_tool_side_effect` rather than invented failure/success;
- domain `needs_human` and domain failure results may retain evidence refs and structured output.

This runtime is still an isolated runtime layer. It has **not** been authorized to drive physical Xiaohongshu/Android actions.

## 2. Stage 3 model-context compaction

Branch: `spike/context-compaction-v1`
Draft PR: `#3`
Base: Stage 2 head above.

### Why it was tested

A real successful analysis recorded **243,276 prompt tokens**, 4,984 output tokens and 45,797 ms provider duration using `deepseek-v4-flash` and prompt contract `tutorial-demand-radar-specific-demand-v2`.

Inspection found that the model request included audit-only shop evidence payloads and also duplicated the `AnalysisOutput` schema between the user prompt and adapter/system prompt.

### Design

`CompactingModelAdapter` creates a deterministic model-only projection at the adapter boundary.

It does **not** replace or compact persisted source evidence. Full evidence remains authoritative for:

- ownership and evidence resolution;
- file/SHA/SQLite trust checks;
- eligibility;
- input digest and trust fingerprint;
- final evidence snapshot;
- post-model grounding;
- cross-account validation;
- human approval.

The compact model view preserves evidence IDs, account ownership and normalized business facts while removing audit-only/raw transport payloads. Duplicate output schema is removed only after exact equality is proven; mismatch fails closed.

### Final verification result

Correct Stage 2 -> Stage 3 comparison:

- context-compaction focused acceptance: **5/5 passed**;
- Stage 2 Runtime acceptance on same merge ref: **23/23 passed**;
- Stage 2 full-backend baseline: **73 failed / 1,444 passed / 20 skipped**;
- Stage 3 full-backend spike: **71 failed / 1,451 passed / 20 skipped**;
- Stage 3-only new regression candidates: **0**;
- final Stage 3 backend regression guard: **success**.

The earlier comparison against `research/xhs-workbench-next` was invalid for Stage-3-only attribution because it included all Stage 2 Runtime changes. The workflow was corrected to use the Stage 2 head as baseline.

### What is not proven

No paid provider A/B was run. The historic 243,276-token result remains the real provider baseline. Structural character/byte reduction must not be reported as an exact provider-token reduction.

Production adoption still requires an explicit new persisted prompt version and intentional `AnalysisService` wiring.

## 3. Pydantic AI dependency-slim experiment

Branch: `spike/runtime-dependency-slim-v1`
Draft PR: `#4`

Hypothesis tested: the Stage 2 addition of the full `pydantic-ai` meta-package might have caused a Qianfan child-process timing regression.

### Validated dependency result

Replacing:

```text
pydantic-ai>=2,<3
```

with:

```text
pydantic-ai-slim>=2,<3
```

produced a clean environment with **47 installed distributions** and preserved Agent Runtime acceptance **23/23**.

The slim environment did not install the full/heavy optional integrations checked by CI, including the full `pydantic-ai` meta-package, Anthropic, OpenAI, Google GenAI, MCP/FastMCP, Logfire and Pydantic Evals.

Therefore slim remains a useful dependency-hygiene candidate.

### Rejected causal hypothesis

Slimming the dependency did **not** fix the timing-sensitive Qianfan test. Repeated runs remained variable. Therefore `pydantic-ai` dependency weight is **not** accepted as the cause of the Qianfan shutdown failure signal.

## 4. Qianfan blocked-worker shutdown diagnosis

Original timing-sensitive test:

`backend/tests/radar/test_qianfan_orchestration.py::test_blocked_browser_worker_cannot_keep_python_process_alive`

An earlier Stage 2-vs-research targeted check showed:

- research baseline: 0/5 failures;
- Stage 2: 5/5 failures.

This initially looked like a Stage 2 regression even though no Qianfan/main/worker source file had changed.

### Phase probe result

A dedicated child-process timing probe showed:

- **10/10** blocked-worker children exited naturally within 5 seconds;
- `QianfanCollectionService.close()` took about **0.05–0.49 s**;
- at close time the only worker besides `MainThread` was `qianfan-ranking`, and it was `daemon=True`;
- close -> natural interpreter exit was about **0.17–0.19 s**;
- total child lifetime varied about **1.82–3.54 s** because Python startup, project imports, SQLite and service initialization happened before shutdown.

The old test starts `communicate(timeout=2)` immediately after `Popen()`. Therefore its two-second budget includes startup/import/database/service setup rather than measuring shutdown only.

### Corrected acceptance

Branch: `fix/qianfan-shutdown-timing-v1`
Draft PR: `#5`
Dedicated run: `32617590841`

A cross-platform stdin/stdout handshake was added:

1. child performs the same setup and starts the permanently blocked worker;
2. child reports `worker-ready`;
3. parent sends `close`;
4. only then does the strict two-second shutdown timer begin;
5. child must emit `lifespan-returned` and exit naturally.

Result: **20/20 passed** on the original Stage 2 full-dependency environment.

Conclusion: current evidence supports a **test timing-boundary bug**, not a Qianfan shutdown regression caused by Stage 2. The shutdown budget was not relaxed.

The old timing-flawed test has not yet been silently skipped, xfailed or given a larger timeout. PR #5 currently preserves the corrected test separately so the old test can be replaced deliberately.

## 5. Canonical Runtime fold-in

Branch: `fix/agent-runtime-foldin-v1`
Draft PR: `#6`
Base: Stage 2 validated head.

### Implemented structural change

The goal is to eliminate the public import-path hazard where:

- package import exposed `Stage2AgentRuntime`;
- direct `backend.app.agent_runtime.runtime.AgentRuntime` still exposed Stage 1 behavior.

Current fold-in implementation:

- original Stage 1 loop preserved as private `_runtime_core.py` scaffold;
- `runtime.py` now owns the hardened public `AgentRuntime` behavior;
- `stage2_runtime.py` is a compatibility alias only;
- package `__init__.py` imports `AgentRuntime` directly from `runtime.py`;
- structural test requires package/direct/Stage2 compatibility imports to be the exact same class object;
- private core is explicitly not the public class.

### Budget hardening

Accepted Stage 2 temporarily mutated shared `self.budget` while a resumed run was driven. The fold-in replaces that with a `ContextVar`-scoped persisted budget so a reconstructed run uses its stored budget without replacing the runtime instance's constructor default.

A new test verifies that a resumed run with a persisted one-call budget exhausts correctly while the runtime default remains 99 calls and a subsequently started run is persisted with the unchanged 99-call default.

### Dynamic verification status

**Not yet dynamically accepted.**

Dedicated workflow run `32617924385` was attempted three times. On every attempt:

- unchanged Stage 2 baseline job: zero steps created, immediate failure;
- fold-in job: zero steps created, immediate failure;
- `actions/checkout` never ran;
- no job logs were available.

The repository's pre-existing Agent Runtime workflow on the same commit showed the same zero-step failure across all runnable jobs. This is an Actions execution-layer blocker, not a test result.

PR #6 must remain draft until compile, focused runtime acceptance and Stage2->fold-in full-backend regression guard actually execute and pass.

## 6. Gate before AgentRun -> durable Job integration

Do **not** start physical XHS/Android Agent integration yet.

Required sequence remains:

1. canonical Runtime fold-in dynamically passes;
2. bind `AgentRun` to the existing durable `Job` state machine;
3. keep `Job` as the unique task truth for lease, lifecycle and final state;
4. keep `AgentRun` as execution trace/checkpoint/model/tool history;
5. any state disagreement must fail closed to human review rather than inventing success;
6. only then evaluate real XHS/Android tools behind explicit permission and recovery gates.
