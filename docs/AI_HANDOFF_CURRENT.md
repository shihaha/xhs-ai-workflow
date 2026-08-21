# Current AI Handoff

> 本文件是 `feature/system-v1` 当前状态的唯一固定交接入口。旧状态文档仅作为历史检查点，不能覆盖本文件中的当前结论。

## Repository State

- branch: `feature/system-v1`
- date: `2026-08-21`
- current engineering state: `Phase A evidence/analyze/review loop complete; the user's approval to continue the current Opportunity into independent product research is persisted through the existing human-review API.`
- architecture alignment for ChatGPT / Codex: `docs/CODEX_NEXT_OBJECTIVE.md`

## What Changed

- 用户在当前对话中已经明确表示：可以批准“七宗罪心理测试数字内容市场机会”继续推进。
- 该批准被定义为：**批准进入独立的产品研究/产品定义工作段**，不是批准某个具体产品方案，不授权自动制作产品，也不授权自动进入内容系统。
- 新增 `docs/CODEX_NEXT_OBJECTIVE.md`，用于统一 ChatGPT、Codex 和人工操作者对完整架构、两个断点和后续接口的理解。
- 已通过现有 Opportunity 人工审核 API 将用户决定持久化为 `approved`；没有直接修改 SQLite，也没有改写历史 evidence/analysis。
- 当前没有把 Phase B（产品研究/定义）或 Phase C（产品制作）自动化强行写入主系统。

## Real Execution Result

- 已读取持久化 analysis `5859e6c9-0485-451b-9152-05896c0d037b`。
- 该分析真实状态为 `succeeded`，一次请求收到模型响应，生成一个待人工审核 Opportunity。
- Opportunity 为“七宗罪心理测试数字内容市场机会”，已通过现有审核 API 从 `pending_review` 转换为 `approved`。
- SQLite 持久化审核时间为 `2026-08-21 08:56:58.175625`，`rejection_reason=null`；evidence level 仍为 `warming_candidate`。

## Current Business State

- candidate pool: `71` 个候选；`2 in_scope`、`31 out_of_scope_physical`、`36 needs_human`、`2 collection_failed`、`0 waiting`。
- in_scope accounts: `2`。
- current analysis: `succeeded`。
- Opportunity: `warming_candidate`。
- persisted review status: `approved`。
- user decision: `approve continuing this Opportunity into product research`，已写入 SQLite/API。
- Phase A discovery/evidence/review goal: 已完成。
- Product research / product definition: 下一独立工作段；当前不要求自动化进主系统。
- Product build: 只有产品定义再次经用户批准后，才建立独立 Codex/AI 制作任务。
- Content system: 只有真实成品完成并通过人工 UAT 后才接入。

## Current Architecture

完整业务链明确为四段：

1. **A — 需求雷达系统（当前仓库）**：回答“什么方向值得继续研究？”并输出 Opportunity。
2. **B — 产品研究 / 产品定义（当前独立工作段）**：回答“这个机会具体应该做什么产品？”；AI 研究，人最终立项。
3. **C — 产品制作（当前独立工作段）**：把被批准的产品定义做成真实可交付成品；软件类可由独立 Codex 项目开发，人做关键视觉/功能/UAT验收。
4. **D — 内容系统（产品完成后接入）**：围绕已经完成的产品做产品分析、关键词、对标、拆解、模板、Skill、日更和内容审查。

当前有意保留两个断点：

- Gap 1: `approved Opportunity → approved Product Definition`
- Gap 2: `approved Product Definition → Finished Product`

不要把内容系统误当成产品制作系统，也不要让 Opportunity 自动跳过 B/C 进入内容生产。

详细边界和接口见 `docs/CODEX_NEXT_OBJECTIVE.md`。

## Current Blockers / Next Work

当前没有需要继续扫榜、继续凑第二个账号或重跑跨账号 analysis 的 blocker。

当前也没有 Opportunity 审核持久化 blocker；该决定已经写入 SQLite/API。

下一步按顺序：

1. 下一项 AI/研究任务：以当前七宗罪 Opportunity 作为第一个真实案例，单独开展产品研究/产品定义。
2. 产品研究完成后，再由用户做第二次决定：`批准具体产品立项 / 继续研究 / 放弃`。
3. 若批准具体产品，建立独立产品制作任务；不要由当前需求雷达后台自动制造。
4. 成品经人工 UAT 后，再设计/执行到内容系统的正式交接。

## Historical Non-blocking Issues

- 历史 analysis `5d4e235a-a762-4aa9-b067-1949d6d0fe05` 曾发生三次 60 秒超时；该失败记录保持不可变，不影响当前成功 analysis。
- 历史 analysis `73728a7c…` 使用现在已不合格的旧店铺证据，不能批准。
- 历史 analysis `37d7fca6…` 不符合当前“共同具体需求”契约，不能批准。
- 历史 XML 换行差异保留为 `legacy_historical_audit_limitation`，不阻塞当前证据闭环和人工审核。

## Tests / Verification

- Opportunity 审核 API：`PASS`；一次 `approve` 请求成功返回 `review_status=approved`。
- SQLite/API 独立复读：`PASS`；Opportunity 为 `warming_candidate + approved`，`reviewed_at=2026-08-21 08:56:58.175625`、`rejection_reason=null`。
- 后端 Opportunity 审核聚焦回归：`2 passed, 75 deselected`。
- 前端 Opportunity 审核页面聚焦回归：`7 passed`。
- 文档来源断言：`PASS`。analysis/Opportunity 的关键持久化字段、共同需求、共同点、关键差异和 rationale 与 SQLite 一致。
- 隐私检查：新增交接文档不记录 API Key、Cookie、Token、password 或完整敏感账号标识。
- 最近一次前端验证：`65 passed`，production build 通过。
- 本次只同步目标/架构并持久化已有人工决定；不冒充 Phase B/C 已实现。

## Important IDs

- analysis ID: `5859e6c9-0485-451b-9152-05896c0d037b`
- Opportunity ID: `caed5776-ef38-4a0a-90fe-57ae2291583e`
- Account A shop job ID: `8507e249-27a3-4339-bf0f-c79c57f9b2e8`
- Account B shop job ID: `fb293aa0-a12e-445d-a627-777da6d256af`
- Account A shop artifact ID: `artifact:1612`
- Account B shop artifact ID: `artifact:1554`

## Next Decision Needed

当前用户层已经决定：**继续研究当前 Opportunity。**

下一次真正需要用户做的新业务决定，不再是“这个 Opportunity 要不要看”，而是：

> 产品研究完成以后，是否批准某个**具体 Product Definition**进入制作。

在该决定之前，不允许自动创建正式产品、自动进入内容系统或自动发布。
