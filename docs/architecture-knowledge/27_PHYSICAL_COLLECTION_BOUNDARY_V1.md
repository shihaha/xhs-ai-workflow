# 27 — Physical Collection Boundary V1

Status: **implemented on `spike/physical-collection-boundary-v1`; controlled real Android UAT completed; stacked Draft PR/CI is the release gate**

Date: 2026-08-24

## Purpose

Stage 6 introduces the first permission-gated physical Agent tool without moving Android/XHS/browser ownership into the Agent Runtime.

The first permitted path is deliberately narrow:

```text
shop.preflight
```

It uses the existing deterministic Radar candidate order and existing `ShopCollectionService` / Android worker. It does **not** introduce a second mobile worker, a generic browser Agent, or a new Project domain.

This slice also records the provider correction made during UAT: **ChatGPT is the primary external reasoning path. Bailian/local models are optional accelerators, not a prerequisite for starting or continuing the workflow.** See `26_CHATGPT_PRIMARY_REASONING_PROVIDER.md`.

---

## Authority flow

### Automatic-provider available

```text
operator selects durable Evidence
  -> Agent orchestration Job + AgentRun
  -> bounded model proposes shop.preflight
  -> ToolRegistry validates exact target
  -> RuleBasedPermissionPolicy = ask
  -> durable HumanAction
  -> operator approves
  -> approval-time target revalidation
  -> new AgentRun continuation
  -> execution-time target revalidation
  -> ShopCollectionService.enqueue(preflight)
  -> durable android_shop_collection child Job
  -> existing Android worker controls XHS/device
  -> Evidence / Artifact / child Job state
  -> parent AgentRun waits at physical_job_pending
```

### No Bailian/local model configured

The absence of a provider must not create a fake running Job and must not disable the workbench start action.

```text
operator selects durable Evidence
  -> one transaction creates:
       agent_orchestration Job (needs_human)
       AgentRun (needs_human)
       safe Evidence seed checkpoint
       AgentRun -> Job binding
       pending ChatGPT Handoff
       pending manual_chatgpt HumanAction
  -> operator uses any ChatGPT conversation + AgentDock xhs-workbench Skill
  -> ChatGPT returns one structured NextAction
  -> backend revalidates tool name + input + current target + permission policy
  -> shop.preflight proposal becomes a new pending HumanAction
  -> NO Android Job exists until the operator explicitly approves
```

The initial ChatGPT result is therefore a **proposal**, not authority.

---

## First physical tool: `shop.preflight`

Implementation: `backend/app/agent_runtime/physical_tools.py`.

Input:

```text
source_date
account_user_id
```

Metadata:

```text
read_only = false
destructive = false
external_side_effect = true
concurrency_safe = false
requires_approval = true
idempotent = false
retry_limit = 0
```

### Exact-target rule

The tool does not accept an arbitrary account merely because that account appears in an old Evidence item.

Before approval and again immediately before enqueue, it calls the existing deterministic Radar candidate funnel and requires:

```text
current next_preflight_candidate(source_date)
  == approved account_user_id
```

If candidate order, prescreen state, source date, or funnel state changed, execution fails closed instead of switching to another account.

This validation happens twice:

1. **pre-approval validation** — before HumanAction/Job authority is mutated;
2. **execution-time validation** — immediately before the physical child Job is enqueued.

---

## Device availability

`shop.preflight` is model-visible only when the existing Android adapter reports available.

The current health path checks the real prerequisites owned by the adapter, including:

- usable ADB;
- one authorized device;
- uiautomator availability;
- XHS installed/foreground as required by the adapter profile.

The real Windows UAT discovered that ADB already existed locally but was not inherited through AgentDock PATH. The UAT used the existing newer ADB binary through local environment configuration. **No machine-specific ADB path is committed to the repository.**

---

## Parent/child durable binding

Once `shop.preflight` queues physical work, the child `android_shop_collection` Job stores a safe `_agent_origin` identity containing the originating AgentRun/tool call identity.

The parent does not keep driving a model while the phone is running. Instead:

```text
physical Job queued
  -> Tool result persisted
  -> parent AgentRun = needs_human / physical_job_pending
  -> parent Job = needs_human / physical_job_pending
  -> physical follow-up worker reads the already-persisted child result
```

This is intentionally a durable wait rather than an in-memory callback.

---

## Physical-result routing

Implementation: `backend/app/agent_runtime/physical_followup.py`.

The reconciler never controls the phone. It only reads already-persisted child Job/Artifact state.

### Child still queued/running

```text
-> waiting
```

No replay and no reasoning task.

### Selector/device/collection failure

If there is no valid scope result because the physical collection failed:

```text
-> preserve physical needs_human boundary
-> DO NOT create ChatGPT Handoff
```

A selector failure is a physical recovery problem, not an AI reasoning problem.

### Deterministic scope result

If the local conservative rule has an unambiguous result:

```text
in_scope
or
out_of_scope_physical
```

then:

```text
-> durable physical_result_ready checkpoint
-> deterministic no-model continuation
-> parent Agent Job terminalized
-> zero ChatGPT Handoffs
-> zero physical replay
```

### Ambiguous scope result

Only `needs_human` scope ambiguity is converted to a ChatGPT reasoning task:

```text
physical_job_pending
  -> request_followup_handoff(...)
  -> manual_chatgpt_required
  -> pending durable ChatGPT Handoff
```

The Handoff references the already-collected child Job artifacts. It never asks Android to collect them again.

---

## ChatGPT evidence path

The installed AgentDock Skill is `xhs-workbench` (validated/installed as 1.1.0 during this Stage-6 work).

Its role is transport/orchestration guidance for a new ChatGPT conversation, not business authority.

Expected use:

```text
@AgentDock 继续工作台任务
```

The Skill recovers durable state instead of relying on old chat context:

```text
status
-> pending
-> read handoff package
-> evidence
-> media (only for handoff-authorized image artifacts)
-> AgentDock.view_image for actual visual inspection
-> structured result
-> accept_result(confirm=true)
```

Important boundary:

- normal workbench projections never expose local artifact paths;
- visual evidence is opened only for an explicitly selected authorized Handoff;
- the image transport locator is not business Evidence and must not be persisted or shown to the user;
- tutorial-required image reasoning is not considered complete unless ChatGPT actually opens the image.

A real Stage-6 phone screenshot was successfully opened through `AgentDock.view_image`, proving this is a real visual path rather than filename/OCR guessing.

---

## ChatGPT scope-result application

Implementation: `backend/app/agent_runtime/chatgpt_scope_result.py`.

The accepted Handoff result contract is bounded to:

```text
classification
reason
evidence_refs
```

Allowed classification:

```text
in_scope
out_of_scope_physical
needs_human
```

Before a result is applied, the backend revalidates:

- Handoff identity/status/revision;
- source AgentRun and current parent Job authority;
- source AgentRun is still the unique latest binding;
- physical child Job identity;
- `_agent_origin` matches the exact source ToolStep;
- cited `artifact:<id>` references are inside the Handoff context;
- artifacts belong to the exact physical child Job/account.

For a resolved classification, `ShopCollectionService.record_account_scope_decision()` persists an auditable decision with `decision_source = chatgpt`, then a deterministic no-model continuation closes the parent Agent Job.

The same accepted Handoff is removed from the worker candidate set after successful finalization, so polling is idempotent.

If ChatGPT itself returns `needs_human`, the system keeps a human boundary rather than manufacturing certainty.

---

## Initial ChatGPT reasoning path

Implementation:

- `ManualChatGPTHandoffService.create_initial_grounded_handoff()`
- `AgentInitialChatGPTDecisionReconciler`
- `AgentInitialChatGPTDecisionWorker`

When no automatic provider is configured, initial grounded orchestration is born directly at a durable ChatGPT wait. There is no temporary running lease and no `AgentInitialDispatchRecord`.

The initial result is parsed as `NextAction`.

Current Stage-6 allowed physical proposal:

```text
shop.preflight
```

The backend then resolves the real ToolSpec, validates arguments, runs `pre_approval_validate`, evaluates the existing permission policy, and requires `ask` before creating the operator HumanAction.

A ChatGPT `finish` action is finalized through a deterministic no-model continuation. It does not construct or call an Agent model runtime.

---

## Restart / uncertain-side-effect safety

The canonical runtime persists the physical ToolStep as `started` **before** invoking the handler.

Therefore:

- if process loss happens after a possible side effect but before committed success, restart sees durable uncertainty;
- `started` / `uncertain` physical Tool evidence is never replayed automatically;
- uncertain external side effect remains `needs_human`.

Real UAT also exposed a narrower case:

```text
HumanAction approved
-> continuation created
-> process interrupted
-> NO ToolStep was ever created
```

A bounded recovery path now permits a **new explicit re-approval HumanAction only if no ToolStep exists for that exact tool_call_id**.

Any ToolStep proof (`started`, `uncertain`, `failed`, `succeeded`, `reused`, etc.) blocks re-approval recovery.

This preserves the rule: **an uncertain side effect is never replayed.**

---

## Workbench projection

The React workbench remains a projection of backend durable truth.

For `shop.preflight`, the backend emits only a safe approval summary and `external_side_effect=true`; raw request/resolution JSON is not projected.

Chinese-first UI makes the physical boundary explicit:

```text
这是真实外部操作。批准后会控制已连接的 Android 设备并访问小红书，不是模拟操作。
```

Second confirmation text:

```text
确认并启动真实设备预检
```

A physical proposal may be approved even when the generic automatic model continuation is unavailable, because the exact approved physical action can execute without Bailian.

---

## Controlled real Android UAT

The controlled UAT used a real authorized Android device and the real XHS app.

### Preflight health

Verified through the project adapter:

```text
Android adapter status = available
detail = ready
uiautomator = available
XHS foreground prerequisite = satisfied
```

### UAT round 1

The authority chain reached the real phone and persisted account/shop evidence, then failed closed with `selector_changed` before collecting the requested product sample.

The captured real shop hierarchy proved that real product cards existed, but `parse_shop_hierarchy()` returned zero products.

Root cause:

- the parser treated the upper shop tabs `分类 / 上新` as a bottom fixed navigation;
- this produced an incorrect bottom occlusion boundary;
- real product cards were filtered as if hidden behind navigation.

The parser was tightened so only lower-screen navigation can establish bottom occlusion.

The same real captured XML changed from:

```text
before fix: 0 products
after fix:  2 products
```

without recollecting that XML.

### UAT round 2

A new legal next candidate was used; the failed first candidate was not replayed.

The full permission-gated path controlled the real phone and completed:

```text
requested preflight products: 3
collected products:           3
```

The physical sequence included account page, shop page, product details, share flow, return-to-shop and scrolling.

The child physical Job persisted **36 durable Artifacts**, including screenshots/hierarchy/result evidence.

The domain result conservatively stopped at scope ambiguity rather than inventing a classification. At the time of this live run the ChatGPT follow-up automation had not yet been implemented; the later Stage-6 code now routes this exact ambiguous category to the durable ChatGPT Handoff path and is covered by regression tests.

This distinction is important: the **physical 3/3 UAT is real hardware evidence**; the subsequently added automatic ChatGPT follow-up/finalization path is verified by deterministic backend tests, not falsely presented as a second real-device run.

---

## Verification status

Local Stage-6 verification on 2026-08-24:

```text
backend/tests/agent_runtime                         147 passed
frontend complete regression                        76 passed
frontend production build                           PASS
desktop/mobile Agent workbench Playwright             2 passed
release-boundary scan                               PASS
git diff --check                                    PASS
```

Physical/ChatGPT focused contracts additionally prove:

- no-Bailian initial start creates a durable Handoff instead of failing;
- no initial dispatch exists on that path;
- ChatGPT physical proposal creates approval only, not an Android Job;
- ChatGPT `finish` executes no model/tool;
- deterministic physical classification creates zero Handoffs;
- ambiguous classification creates exactly one authoritative Handoff;
- accepted ChatGPT scope result persists a domain decision and closes the parent without phone replay;
- successful result is not consumed again by the polling worker.

The wider shops/radar Windows run currently reports 133 passes plus 3 failures. The exact same 3 tests fail identically on the detached PR-22 baseline worktree, so they are **pre-existing Windows/local baseline behavior, not Stage-6 regressions**. Do not modify business logic merely to hide this baseline discrepancy; GitHub/Linux baseline-vs-spike comparison remains authoritative.

---

## Non-goals

Stage 6 V1 does **not** add:

- a singular `qianfan.collect_rank_scope` refactor;
- `shop.collect_evidence_sample` or full-shop Agent physical tools;
- generic browser ownership by Agent;
- direct Android adapter calls from Agent;
- AG-UI streaming;
- Project entity / Stage 7 business journey;
- automatic replay of uncertain physical actions;
- mandatory Bailian/local-model dependency;
- raw artifact paths in ordinary workbench projections.

The next stage must not begin until this stacked branch has dedicated CI and broad regression evidence reviewed.
