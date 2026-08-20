# Project Identity

project_id: xhs-ai-workflow
repository: shihaha/xhs-ai-workflow
branch: feature/system-v1
remote_url: https://github.com/shihaha/xhs-ai-workflow.git
source_codex_thread: codex://threads/01a00ec6-75b8-72e3-9281-48c69ffe26fb
last_updated: 2026-08-20 (Phase A handoff)

> 状态口径：本文以 `feature/system-v1` 的 Phase A 提交、隔离运行数据库和本机证据目录为依据。自动测试、受控夹具和真实平台 UAT 严格分开。下方“Phase A 完成报告”是当前权威增量，覆盖本文较早的单账号工作流快照。当前结论是 **Phase A software implemented / real UAT not passed（软件已实现，真实验收未通过）**。原 `cli_failed` 已定位并恢复 control；当前真实阻塞是第二账号观察到的 18 个商品链接没有持久化，可信样本为 `0/3`。

# 1. 项目是干什么的

这是一个在 Windows 本机运行的“小红书需求雷达 + 内容生产工作台”。它把原教程中的业务流程改写为可追踪、可恢复、状态来自真实数据库和文件的系统：

1. 从小红书千帆读取固定 8 个榜单范围。
2. 保存原始榜单证据并按固定规则计算候选账号。
3. 通过 ADB/uiautomator2 控制一台已授权安卓手机，只读进入账号店铺并采集商品链接与界面证据。
4. 通过受限的 `xhs-cli` 读取公开账号、笔记和关键词搜索结果。
5. 用阿里云百炼做有证据引用的结构化分析、文案和图片/视觉建议。
6. 由人工审核后导出待发布 ZIP；V1 不自动发布、点赞、评论或收藏。

项目是在复刻教程的**业务效果和证据链**，不是复制教程外观，也没有直接把教程附件当作可运行产品。教程中的 macOS/Bash/Ego/Obsidian 路线被替换为 Windows、FastAPI、React、SQLite、Playwright、ADB/uiautomator2 和受控 CLI 适配器。

当前重点只收口一个真实闭环：`8 个千帆范围 → 真实榜单账号 → 真机商品 2/2 深度核验 → 该账号笔记 → 分析/机会 → 产品/内容 → 百炼图片与视觉建议 → 人工审核 → ZIP`。

暂时不做：自动发布和互动、多账号/多设备、多租户 SaaS、绕过验证码或风控、原生 Obsidian 插件、教程中的完整视频研究流水线。

用户最终通过本地 Web 工作台使用：`/radar` 看榜单与候选、`/accounts/<user-id>` 看账号证据、`/opportunities` 看分析机会、`/content` 生成和审核内容、`/jobs` 看真实任务状态、`/status` 看本机依赖状态。

# 2. 当前真实架构

```text
用户（本机浏览器 + 人工审批）
  ↓
React/Vite 工作台
  ↓ HTTP /api/v1
FastAPI 应用
  ├─ 持久任务/日志/证据服务 ───────────────┐
  ├─ 千帆 Playwright 采集适配器            │
  ├─ 小红书只读 xhs-cli 适配器             │
  ├─ Android ADB/uiautomator2 店铺适配器   │
  ├─ 百炼文本/图片/视觉适配器               │
  └─ 分析/机会/产品/内容/审核/导出服务       │
                  ↓                         │
              SQLite（唯一业务事实源） ←────┘
                  ↓
外部 runtime：证据、截图、UI XML、图片、ZIP、日志、浏览器 Profile
```

真实角色说明：

- 主 Agent：仓库外部的 Codex 线程“评估系统复刻方案”。它负责开发、诊断和真实 UAT，但不是产品运行时的一部分。
- 子 Agent：只在开发阶段由主 Codex 临时派发过独立实现/验证任务；仓库运行时没有可枚举的子 Agent 注册表。
- Runtime Worker：Android 店铺任务、XHS 账号/搜索任务、内容媒体任务和清理任务是确定性 worker/service，不应冒充 AI Agent。
- Skill：仓库中没有可加载的 `SKILL.md` 或产品 Skill 注册机制。`.superpowers/sdd` 是开发过程记录，不是产品运行时 Skill。
- SQLite：唯一业务数据库；任务、证据引用、榜单、账号笔记、分析、机会、内容、审核和包状态都从这里读取。
- 文件 runtime：保存截图、UI hierarchy、原始捕获、内容图片、ZIP、日志和浏览器/CLI 状态；不进入 Git。
- 手机：只读打开小红书账号、店铺、商品详情和分享面板。
- ADB/uiautomator2：连接设备、检查前台应用、导航、截图、保存 UI XML、读取受控复制结果、恢复输入法。
- Playwright：使用独立持久浏览器 Profile 采集千帆固定 4 榜 × 2 维度；前端 E2E 也使用 Playwright，但那是受控测试，不是真实平台证明。
- Obsidian：当前不参与运行。项目仅参考教程 Obsidian 信息架构，实际展示由 React 工作台完成。

# 3. 当前完整工作流

## 步骤 1：千帆八榜采集

- 执行者：`QianfanPlaywrightAdapter` + radar collection service。
- 输入：固定四榜 × 两维度、日期、每范围期望数量、受控登录 Profile。
- 处理：监听并验证当前页面的规范 `POST` 响应，拒绝不匹配范围或页码的响应。
- 输出：8 个持久任务、8 个原始证据、8 个榜单快照和标准化榜单条目。
- 保存位置：SQLite `radar_qianfan_collections`、`radar_rank_snapshots`、`radar_rank_items`；runtime evidence。
- 下一步：任务成功后由 Radar 页面读取账号聚合与评分。

## 步骤 2：账号评分与候选

- 执行者：`backend/app/features/radar/scoring.py` 和 radar service。
- 输入：标准化榜单条目及阅读、点击、支付、GMV、跨榜/跨日等事实。
- 处理：固定、可解释的评分与去重。
- 输出：候选账号投影及评分明细。
- 保存位置：原始榜单事实留在 SQLite；候选列表由查询计算，不伪造独立进度。
- 下一步：用户选择真实账号进入账号/店铺采集。

## 步骤 3：安卓真机店铺采集

- 执行者：Android shop worker + `android_device.py`。
- 输入：榜单账号、设备 ID、期望商品数、selector profile、可选 verification directory。
- 处理：账号主页 → 店铺 → 商品详情 → 分享面板 → 复制链接 → 返回店铺；每个阶段保存截图和 UI XML。
- 输出：唯一商品链接、缺失/拒绝/溢出事实、截图/XML、`result.json`。
- 保存位置：SQLite `jobs`、`job_artifacts`；runtime evidence。
- 下一步：严格验证目录必须为每件商品提供 `detail.json`、本地图片和 SHA-256 manifest；全部一致后才能通过 N/N。

## 步骤 4：账号笔记与关键词读取

- 执行者：XHS reserved worker + pinned `xhs-cli` readonly wrapper。
- 输入：受信登录状态、账号 ID、期望笔记数或关键词/期望搜索数。
- 处理：固定 `status/whoami/user/user-posts/search` 只读命令；严格限制 argv、环境、输出大小、超时和证据哈希。
- 输出：账号资料快照、公开笔记、搜索结果原始 artifact。
- 保存位置：`xhs_account_profiles`、`xhs_account_profile_snapshots`、`xhs_account_notes`、`xhs_account_snapshot_notes`、`job_artifacts`。
- 下一步：人工确认证据仍可信，再选择 `account-note:*` 和 shop 证据做分析。

## 步骤 5：结构化账号分析与机会

- 执行者：analysis service + Bailian text adapter。
- 输入：明确允许的持久 evidence IDs 和 evidence snapshot。
- 处理：发送固定 prompt/schema；Pydantic 严格校验；失败不会写成成功。
- 输出：分析、引用、机会卡。
- 保存位置：`analyses`、`opportunities`。
- 下一步：用户选择机会并创建产品。

## 步骤 6：产品与内容草稿

- 执行者：content service + Bailian text adapter。
- 输入：成功机会、目标用户、受管理材料、可信证据。
- 处理：生成有引用的版本化草稿和图片计划。
- 输出：产品、材料、内容条目、内容 revision。
- 保存位置：`content_products`、`content_product_materials`、`content_items`、`content_revisions`。
- 下一步：按图片计划生成图片或提交人工审核。

## 步骤 7：图片生成与视觉建议

- 执行者：media worker + Bailian image/vision adapters。
- 输入：当前 revision、图片计划条目、受管理材料 ID。
- 处理：异步生成图片、下载/解码/哈希/落盘；视觉模型返回严格结构化建议。
- 输出：受管理 `output_image`、media run、视觉建议 artifact。
- 保存位置：`content_media_runs`、`content_product_materials`、`job_artifacts` 和 runtime content。
- 下一步：人工逐图检查；视觉模型不能自动批准。

## 步骤 8：人工审核与导出

- 执行者：用户 + content review/export service。
- 输入：当前 revision、人工视觉检查、通过/退回/重新生成决定。
- 处理：记录审计决定；只有当前 revision 明确批准后才能打包。
- 输出：待发布 ZIP、manifest、正文、图片、来源和审核记录。
- 保存位置：`content_reviews`、`content_packages`、runtime content。
- 下一步：用户在系统外人工发布；V1 不执行平台发布。

# 4. 当前目录结构

```text
D:\AI_WORKSPACE\xhs-intelligence-workbench
├─ backend/app/
│  ├─ adapters/        # 千帆、XHS CLI、Android、百炼和统一契约
│  ├─ api/             # health 与通用 jobs API
│  ├─ features/        # radar、shops、xhs、analysis、content、media
│  ├─ db.py            # SQLite schema/migrations/完整性约束
│  ├─ main.py          # FastAPI 组装、worker 启动与恢复
│  └─ settings.py      # 仅从环境读取运行配置
├─ backend/tests/      # 63 个后端测试文件；live gate 默认关闭
├─ frontend/src/       # React 页面、API client、8 个单元测试文件
├─ frontend/e2e/       # 空数据库到 ZIP 的受控 Playwright E2E
├─ scripts/            # run-local.ps1、verify.ps1
├─ tools/              # release boundary 扫描工具
├─ docs/               # 设计、状态、UAT、运行手册、来源审计
├─ .superpowers/sdd/   # 已跟踪的历史开发/复审记录；不是运行时 Skill
├─ research/           # 当前主工作区未跟踪 WIP；本次不提交
└─ PROJECT_STATUS.md   # 本交接与永久项目身份
```

外部运行目录由本机受信配置指定（具体路径不写入 GitHub）。其中包含 SQLite、evidence、content、logs、浏览器 Profile、CLI 状态和真实 UAT 隔离目录；全部不进入 GitHub。

# 5. 已经真正完成的功能

- ✅ 已完成并真实运行：千帆 8/8 范围真实采集，每范围 10/10，共 8 快照、80 条榜单项。
- ✅ 已完成并真实运行：受信当前账号资料 + 3 篇公开笔记的只读 live gate；空搜索结果精确 0/0。
- ✅ 已完成并真实运行：Vivo V2303 / Android 16 真实进入榜单账号店铺并观察、采集 2/2 商品链接，保存 16 个截图/XML 证据；深度商品图片验证尚未完成。
- ✅ 已完成并真实运行：百炼图片生成和视觉分析 live gate（真实 `wan2.6-t2i`、`qwen-vl-max`）。
- 🟢 代码已完成并通过测试：FastAPI/React/SQLite 工作台、持久 jobs/evidence、评分、N/N 校验、账号/笔记持久化、分析/机会、产品/内容、审核、导出、媒体 worker、隔离清理和恢复机制。
- 🟢 代码已完成并通过测试：当前隔离基线 `1381 passed, 3 skipped`（2026-08-20，本次观察员新鲜执行；live gate 未启用）。
- 🟡 部分完成：百炼文本真实调用 HTTP 200，但输出未通过严格 `AnalysisOutput` 校验；真实分析仍未成功。
- ✅ 已完成并真实运行：同批次两张真实商品详情截图、两条链接、`detail.json`、图片和 SHA-256 manifest 已通过现有严格校验，结果为 `2/2/2、0 missing、complete=true`。
- 🟡 部分完成：上述严格验证尚未绑定回一个新的持久 `succeeded` job；原 `android-live-uat-20260820-09` job 仍如实保留为 `needs_human`、verified 0/2 历史事实。
- 🟡 部分完成：完整业务闭环只在受控 fixture/E2E 通过，尚未用同一榜单账号的全套真实证据跑通。
- 🔵 主 Codex 当前正在开发：进入同一榜单账号的公开资料/笔记只读采集；首次 20 秒预检超时，正在按既定边界做一次 60 秒重试。
- ❌ 尚未开始：非空关键词真实搜索、评论采集、视频下载、关键帧、视频转写、七天稳定性 UAT、原生 Obsidian 插件。

# 6. 主 Codex 当前正在做什么

源线程当前为 active，Android 商品证据的同批次严格校验已经完成，最近一轮正在执行下一项：同一榜单账号的公开资料/笔记只读采集。

已做到：

- 真机两件商品详情页均已目视确认是真实且互不相同的商品，不是占位图。
- 每件商品已有详情截图、分享页、UI XML 和当次复制链接。
- 已发现小红书短链每次复制可能变化，且两轮店铺商品集合也可能变化，不能用上一轮短链/图片预填下一轮任务。
- 主 Codex 已使用“同一批次链接 + 同一批次详情截图”构建验证目录并通过严格校验：`2/2/2、0 missing、complete=true`。
- 随后开始同一榜单账号笔记预检；20 秒边界第一次超时，没有写入账号/笔记事实，当前只允许再做一次 60 秒重试。

当前涉及的关键文件/事实：

- `backend/app/adapters/android_device.py`
- `backend/app/features/shops/service.py`
- `backend/tests/shops/test_shop_collection.py`
- `backend/tests/shops/test_nn_verification.py`
- `docs/UAT_CHECKLIST.md`
- `docs/IMPLEMENTATION_STATUS.md`
- runtime `android-live-uat-20260820-09` 的 SQLite 和 17 个 job artifacts

尚未完成：

- 将已通过的 2/2 严格验证绑定到新的持久成功任务（原历史 job 不改写）。
- 榜单账号本人的公开笔记链路。
- 真实文本分析到 ZIP 的完整闭环。

刚出现的问题：短分享链不是稳定商品主键。当前线程没有把它误报为已解决，也没有要求暂停开发。

# 7. 教程功能完成度

| 教程能力 | 状态 | 真实结论 |
|---|---:|---|
| 千帆 4 榜 × 2 维度 | ✅ | 真实 8/8、每范围 10/10 已入库 |
| 榜单原始证据与账号评分 | ✅ | 代码、测试和真实快照均存在 |
| 候选账号工作台 | 🟢 | 页面和查询完成；仍需同一真实账号闭环 |
| 安卓真机进入店铺 | ✅ | Vivo/Android 16 已真实完成 |
| 全商品唯一链接采集 | 🟡 | 当前样本 2/2 collected；长期完整性未证明 |
| 商品详情、独立图片、manifest N/N | ✅ | 同批次严格验证 `2/2/2、0 missing、complete=true`；原历史 job 仍保留 verified 0/2 |
| 账号资料和笔记采集 | 🟡 | 当前登录账号 1+3 live 通过；榜单账号链路未过 |
| 非空关键词笔记搜索 | ❌ | 仅证明空结果 0/0 |
| 评论采集 | ❌ | 无生产表、API 或 live 证据 |
| 图片下载/管理 | 🟡 | 内容图片已实现并 live；商品图片 N/N 正在收口 |
| 视频下载 | ❌ | 未实现 |
| 关键帧 | ❌ | 未实现 |
| 视频转写 | ❌ | 未实现 |
| AI 账号分析 | 🟡 | 代码/受控测试完成；百炼文本 live 失败 |
| 跨账号需求合并/机会 | 🟢 | 代码与受控测试完成；真实闭环未跑 |
| 产品/内容生成 | 🟢 | 代码与受控 E2E 完成；真实文本链未跑通 |
| 图片生成与视觉建议 | ✅ | 百炼真实 gate 已通过，仍由人工审批 |
| 独立审核与返工 | 🟢 | 审核记录、退回、重新生成已实现；真实闭环未跑 |
| ZIP 待发布包 | 🟢 | 受控 E2E 通过；真实包待验收 |
| Obsidian 工作台 | 🚫 | 当前明确不用；React 工作台替代 |
| 自动发布/点赞/评论 | 🚫 | V1 明确禁止 |
| 七天稳定运行 | ❌ | not_run |

# 8. 数据结构

| 数据 | 当前格式与字段 | 真实状态 |
|---|---|---|
| 小红书笔记 | SQLite `xhs_account_notes`：`note_id/user_id/source_url/title/summary/published_at/public_interactions_json/raw_evidence/raw_digest/job/artifact/collected_at`；另有不可变 snapshot membership | 已实现；当前账号 3 条 live |
| 评论 | 无生产表/文件规范 | 未实现 |
| 图片 | Android 截图为 PNG + `job_artifacts`；内容图片在 `content_product_materials` 保存 path/SHA-256/size/media_type/kind；商品验证要求 `images/* + detail.json image_manifest` | 内容图片完成；商品图片验证中 |
| 视频 | 无生产表/文件规范 | 未实现 |
| 关键帧 | 无生产表/文件规范 | 未实现 |
| 转写 | 无生产表/文件规范 | 未实现 |
| 店铺商品 | 当前主要存在 Android `shop_collection_result` JSON artifact：唯一链接、计数、missing/rejected/overflow/evidence；深度目录以 `detail.json + images + SHA-256 manifest` 验证，不是独立商品表 | 2/2 observed；同批次严格 verifier 已 complete；原持久 job 未回写 |
| 内容产品 | SQLite `content_products`：opportunity/name/target_user；材料在 `content_product_materials` 版本化保存 | 代码/受控 E2E 完成 |
| 关键词 | 搜索词在 job input 与搜索 artifact；无独立关键词主表 | 空结果 live；非空未证明 |
| 账号 | 榜单账号来自 `radar_rank_items`；公开账号 current + immutable snapshots 在 `xhs_account_profiles` / `xhs_account_profile_snapshots` | 部分 live |
| 分析结果 | `analyses` 保存类型、账号范围、provider/model、prompt、input digest、evidence snapshot、结构化 output、usage/duration/attempt/error；机会在 `opportunities` | 代码完成；文本 live 未成功 |
| Skill | 无运行时 Skill 数据表或 `SKILL.md` | 未实现且当前不需要 |
| 任务状态 | SQLite `jobs` + `job_logs` + `job_artifacts`；状态固定 queued/running/needs_human/succeeded/failed/cancelled，含进度、stage、error、retry、lease 和时间戳 | 已实现并真实使用 |

主要持久格式：业务事实使用 SQLite/JSON 列；设计/状态使用 Markdown；证据使用 JSON、PNG、XML、ZIP；未发现生产 CSV/YAML 数据层。

# 9. 去重和恢复机制

| 机制 | 状态 | 实现位置与说明 |
|---|---:|---|
| 笔记去重 | ✅ | `features/xhs/models.py` 以 `note_id + user_id + collection_job_id` 约束，同一快照位置唯一 |
| 跨关键词去重 | 🟡 | 适配器有稳定 note identity/URL 归一化，但没有独立跨关键词全局主表和真实非空搜索证明 |
| 评论去重 | ❌ | 评论未实现 |
| 榜单去重 | ✅ | `radar_rank_snapshots` 范围唯一；`radar_rank_items` 以 snapshot + stable_key 唯一 |
| 商品链接去重 | ✅ | `shops/service.py` 对 source URL 和 product directory 去重并列出 duplicate/overflow |
| 增量采集 | 🟡 | 支持新任务/新不可变快照和历史审计；不是通用“自动只补变化”调度器 |
| 断点续跑 | 🟡 | job/lease/恢复状态持久化；UI 可按原 job ID 继续读取，平台任务通常以新审计任务重试，不从任意 UI 点击精确续位 |
| 失败重试 | ✅ | 模型有有界 retry；其他失败保留证据并显式新建/恢复任务，不无限重试 |
| 任务中断恢复 | ✅ | `JobService`、各 worker startup recovery、lease expiry；遗留 running 变为可见非成功状态 |
| 只补缺失数据 | 🟡 | N/N 会给 missing list；通用自动补采尚未在真实平台证明 |
| 手机断开恢复 | 🟡 | 代码和自动测试覆盖；真实断线恢复/七天 UAT 未跑 |
| 账号掉线恢复 | 🟡 | login_required/needs_human 已实现；真实过期恢复未完成七天 UAT |
| 视频转写失败恢复 | ❌ | 视频/转写未实现 |
| 任务状态持久化 | ✅ | `jobs/job_logs/job_artifacts` + SQLite WAL/lease/recovery |
| 文件提交不确定恢复 | ✅ | `xhs_artifact_promotion_journal` 与内容 quarantine 保留文件身份，不误删唯一证据 |

# 10. Agent 列表

## 外部主 Codex（实际存在，但不在仓库运行时）

- 名称：评估系统复刻方案。
- 职责：需求分解、代码实现、测试、真机/平台 UAT、问题修复和提交。
- 对应文件：无单一 Agent 文件；来源线程为本页 `source_codex_thread`。
- 输入：用户指令、教程材料、仓库、手机/浏览器/百炼环境。
- 输出：代码、测试、提交、UAT 证据和状态文档。
- 谁调用：用户。
- 什么时候调用：开发和真实验收期间。
- 是否实际运行过：是，当前仍 active。

## 产品运行时 Agent

当前数量：**0**。仓库没有 Agent registry、Agent prompt package、Agent-to-Agent 调度器或可枚举子 Agent 文件。下列能力是普通适配器/worker，不应写成 Agent：Qianfan collector、XHS readonly collector、Android shop worker、Bailian analysis/content/media adapter、artifact cleanup worker。

教程中的“分析 Agent / 生成 Agent / 审查 Agent”目前被实现为受控模型调用 + 持久服务 + 人工审核边界；审查决定由用户完成，不是另一个常驻自主 Agent。

# 11. Skill 列表

当前产品运行时 Skill 数量：**0**。

- 仓库没有 `SKILL.md`。
- `.superpowers/sdd` 保存开发计划、progress 和复审报告，不是可被产品调用的 Skill。
- 教程附件中的 Agent 提示词/Skill 只被当作设计来源，没有直接安装到运行时。
- 外部 Codex 会话使用的本机技能不属于本项目代码，也不计为项目功能。

# 12. 测试情况

## 真实环境运行成功

- 千帆：8/8 范围、每范围 10/10、80 条榜单项。
- XHS 当前账号：1 个 profile + 3 篇公开笔记；空搜索 0/0。
- Android：设备授权、前台小红书、店铺路径、2/2 商品链接、16 个截图/XML；最终商品图片 N/N 未成功。
- 百炼图片/视觉：真实生成与严格视觉结构通过。

## 自动测试成功

- 本次观察员在隔离 worktree 新鲜运行后端：`1381 passed, 3 skipped`，耗时 271.09 秒。
- 仓库记录的前端单元测试、生产构建和受控空库 E2E 已通过；本次未重新运行这些既有门禁，因为只新增 Markdown 且没有安装隔离 worktree 的 Node 依赖。
- 后端 63 个 test 文件；前端 8 个 unit test 文件，另有 Playwright E2E。

## 代码存在但没跑完真实验收

- 排名账号的账号/笔记链路。
- 非空关键词搜索。
- 商品独立图片/manifest 2/2。
- Bailian 文本严格分析。
- 真实分析 → 机会 → 产品 → 内容 → 审核 → ZIP。
- Cookie 失效、手机断线、进程重启和七天稳定性矩阵。

## 当前测试失败/非成功

- 百炼文本 live：HTTP 200 后 `model_output_invalid`。
- Android 原真实 job：`needs_human/product_evidence_verification_pending`，verified 0/2；后续同批次独立严格校验已 complete，但还不是新的持久成功 job。
- 评论、视频、关键帧、转写：没有测试对象，因为尚未实现。
- Obsidian：当前明确不是运行依赖。
- Agent 调用：没有产品运行时 Agent，不能宣称测试过。

# 13. 当前已知问题

1. Bug/兼容性：商品短分享链每次复制可能变化，不能作为跨批次稳定商品主键。
2. 不稳定环节：小红书/千帆页面、登录态、selector、Android 16 剪贴板和浏览器启动时间可能变化。
3. 临时实现：商品详情没有独立业务表，当前以 Android result artifact + 外部严格验证目录表达。
4. 手机依赖：必须保持手机解锁、屏幕常亮、USB 调试授权和小红书登录；系统不绕过锁屏。
5. 登录依赖：千帆持久 Profile 与 XHS CLI Cookie 都在 runtime；失效后需要人工重新登录。
6. 选择器风险：千帆和手机 selector 都必须按实际布局验证，变化时进入 `needs_human`。
7. 重复采集风险：同一商品短链变化使简单 URL 去重不足；当前需同批次证据或更稳定身份。
8. 数据丢失风险：主设计已有 journal/quarantine/哈希保护，但 runtime 尚无本文确认的异盘备份方案。
9. Token/费用：百炼文本、图片、视觉会产生真实调用费用；live gate 必须显式开启并保持有界。
10. 上下文膨胀：开发历史很长，`.superpowers/sdd` 记录较多；以 SQLite/UAT 文档和本状态页为交接入口。
11. 人工操作：登录、验证码、锁屏、视觉审批、最终发布必须人工完成。
12. 安全：`.env`、Cookie、浏览器 Profile、手机证据、runtime 数据不得上传；当前 `.gitignore` 已覆盖这些常见路径，但每次提交仍要扫描。
13. 未完成测试：真实非空搜索、榜单账号笔记、完整闭环和七天 UAT。
14. 验证脚本限制：仓库根目录存在真实 `.env` 时，某些测试会继承本机配置；干净 worktree/临时目录可隔离，不能把配置污染当成产品失败或通过。

# 14. 当前重要设计决定

- Windows 本机、单用户、单账号、单安卓设备、SQLite，不建设云 SaaS。
- 所有状态必须来自数据库和真实文件；测试 fixture、页面和按钮不能冒充业务完成。
- SQLite 是唯一业务事实源；React 是工作台；Obsidian 不作为运行依赖。
- 确定性代码负责点击、状态、去重、计数、重试和恢复；模型负责受限的理解与生成。
- Qianfan 只采固定 4 榜 × 2 维度的规范响应，不允许客户端传 selector/URL/script。
- XHS CLI 使用固定只读 wrapper、受信外部状态、固定 argv、隔离环境和有界输出。
- Android 遇到锁屏、登录、验证码、布局变化、设备断开时显式非成功，不绕过安全机制。
- 商品必须 N/N：链接数、目录数、图片 manifest 和 SHA-256 全部一致才算完成。
- 百炼结构化输出必须通过 Pydantic；不能为迎合模型而放松事实/引用校验。
- AI 视觉建议不能自动满足人工视觉检查或批准内容。
- V1 只导出待发布 ZIP，不进行小红书发布或互动。
- 文件删除使用持久队列、同卷 quarantine 和恢复记录，不直接删除唯一证据。

# 15. 当前最合理的下一步

- P0：由原主 Codex完成正在运行的同一榜单账号公开资料/笔记链路，核对 profile、N/N、来源链接、artifact hash 和 SQLite；观察员不抢做。
- P0：把已通过的商品 2/2 严格验证作为新的持久审计任务记录，不覆盖原 `needs_human` 历史 job。
- P1：修正或适配百炼文本严格结构输出，然后用“已核验 shop + account-note”完成一次真实分析/机会。
- P1：继续真实产品/内容/百炼图片/视觉/人工审批/ZIP，并逐项核对 manifest、文件和数据库。
- P2：在闭环成功后做断线、登录失效、重启恢复和七天观察；当前不新增评论/视频/Obsidian/自动发布范围。

# 16. 给 ChatGPT 的交接摘要

## 给 ChatGPT 的交接摘要

这是一个 Windows 本机小红书 AI 工作流，永久 ID 为 `xhs-ai-workflow`。真实代码位于 GitHub 私有仓库 `shihaha/xhs-ai-workflow`，当前开发分支 `feature/system-v1`。系统使用 React + FastAPI + SQLite，把千帆榜单、账号/笔记、安卓店铺商品、百炼分析/图片、内容审核和 ZIP 导出串成可审计流程。

运行时不是多 Agent 产品：主 Codex 在仓库外负责开发；产品内部是受控 adapters/workers 和人工审批。也没有运行时 Skills 或 Obsidian 插件。SQLite 是唯一业务事实源，runtime 文件保存证据，二者都不应被演示数据替代。

已真实完成：千帆 8/8 × 10/10；当前登录 XHS 账号 1 个 profile + 3 篇笔记；Android 真机进入榜单账号店铺并采集 2/2 商品链接和 16 个截图/XML；百炼图片与视觉 live gate。

商品同批次严格校验已经得到 `2/2/2、0 missing、complete=true`；原持久任务仍诚实地是 `needs_human`、verified 0/2，没有被事后篡改。主 Codex 当前正在采集同一榜单账号的公开资料/笔记；第一次 20 秒预检超时，正在进行一次 60 秒有界重试。

尚未完成：榜单账号笔记、非空关键词搜索、百炼文本成功、真实分析到 ZIP 闭环、评论/视频/关键帧/转写、七天 UAT。最大问题是商品短链会变、文本模型输出不符合严格 schema、真实 E2E 尚未闭环。

下一步不要扩展功能：先完成商品 2/2 核验，再跑同一账号笔记，再跑真实分析/内容/ZIP，最后做稳定性 UAT。

## ChatGPT 优先检查文件

1. `PROJECT_STATUS.md`
2. `SYSTEM_SPEC_AND_ACCEPTANCE.md`
3. `docs/UAT_CHECKLIST.md`
4. `docs/IMPLEMENTATION_STATUS.md`
5. `backend/app/main.py`
6. `backend/app/db.py`
7. `backend/app/adapters/android_device.py`
8. `backend/app/features/shops/service.py`
9. `backend/app/adapters/qianfan_playwright.py`
10. `backend/app/features/xhs/service.py`

# Current WIP

观察快照：2026-08-20 01:48 +08:00。

- 主工作区分支：`feature/system-v1`。
- 主工作区 HEAD：`98b8534 fix: validate Android shop collection live`。
- tracked modified files：无。
- untracked：`research/`（1 个既有候选评估文件；本次不读取为运行事实、不提交、不删除）。
- `git diff --stat`：空。
- 主 Codex runtime WIP：榜单账号公开资料/笔记只读预检与持久化；当前尚未形成已跟踪源码 diff。
- 最近与当前任务有关、已经提交的文件：`backend/app/adapters/android_device.py`、`backend/tests/shops/test_shop_collection.py`、`docs/UAT_CHECKLIST.md`、`docs/IMPLEMENTATION_STATUS.md`。
- 最近真实数据：`android-live-uat-20260820-09/workbench.sqlite3` 有 1 个 `needs_human` job、17 个 artifacts；其后同批次商品 verifier 已 `complete=true`。真实 runtime 不上传 GitHub。

如果主 Codex 后续产生新提交，应更新本节 HEAD/WIP 和第 6 节，不要把运行中修改误写成稳定完成。

---

## Phase A 完成报告

> 本节是 2026-08-20 的权威 Phase A 交接，覆盖上方较早的单账号流程和
> Current WIP 快照。结论：**Phase A 软件完成；真实跨账号业务验收未完成；
> Phase B 未开始。**

1. **最终业务变化**：单账号 `account_report` 只能保存观察信号；
   `product_cluster/account_opportunity` 必须选择至少两个不同账号。模型只提
   聚类和引用，账号覆盖、商品/笔记归属和证据等级由服务端验证和计算。
2. **数据库与迁移**：没有新增业务表；`opportunities` 新增
   `review_status/evidence_level/supporting_accounts_json/
   supporting_products_json/supporting_notes_json/reviewed_at/
   rejection_reason`。版本化迁移
   `phase_a_cross_account_opportunities_v1` 保留旧 ID，把无法证明为跨账号的
   历史机会标为 `rejected/legacy_ungraded`。SQLite triggers 固化证据不可变和
   `pending_review` 的单向审核终态。
3. **API 变化**：跨账号 `AnalysisCreate.account_user_ids` 至少两个不同账号；
   `OpportunityRead` 返回审核状态、服务端等级、支撑账号/商品/图片/笔记；新增
   `POST /api/v1/opportunities/{id}/review`。候选账号在评分指标不足时返回
   `score_status=insufficient_metrics`、真实出现次数和最佳名次，不伪造分数。
4. **后端核心变化**：analysis schema/service 验证完整逐账号 shop + note
   ownership；content service 只接受人工批准且证据合格的机会；radar service
   对缺失指标做事实排序；db migration 保留历史并 fail closed。
5. **前端变化**：账号页删除单账号“生成机会”；Opportunities 页面提供完整
   账号多选、证据完整度、跨账号聚类、支撑账号/商品/图片/笔记展示和人工
   approve/reject；明确显示 Phase B 尚未开始且不提供产品入口。
6. **单账号限制**：schema 拒绝少于两个不同账号的跨账号分析；即使模型返回
   opportunity，`account_report` 也会以严格失败收口，不能写机会行。
7. **两/三账号等级**：服务端按验证后的不同账号数计算；2 个为
   `warming_candidate`，3 个及以上为 `validated_candidate`。自动测试覆盖模型
   伪报状态、重复账号和错绑 evidence。
8. **审核状态机**：合格候选自动写 `pending_review`；人工 `approve` 写
   `approved`，`reject + 非空原因` 写 `rejected`。终态不可回退、互转或编辑
   支撑证据，否决行保留审计。
9. **产品门禁**：产品创建必须引用 `approved` 且等级为 warming/validated 的
   机会，并重新核验 analysis output、引用和 shop/account-note trust。未批准、
   rejected、legacy 或证据漂移均拒绝。Phase A UI 没有创建产品操作。
10. **真实账号数**：有 **2 个**账号具备可信 profile/note 事实，但只有
    `real-account-A` 同时具备可信 shop 事实，因此仍不足以运行真实跨账号聚类。
    真实千帆隔离导入有 8 个范围、80 条榜单项和 71 个候选账号投影；候选投影
    不等于可信账号。
11. **每账号商品/笔记**：`real-account-A` 有 2 个严格验证商品和 62 篇可信公开
    笔记；shop 事实为 expected/discovered/succeeded `2/2/2`、missing `0`、
    `complete=true`。第二账号有 10 篇可信 latest-note 样本、0 个可信商品。其
    Android 任务观察到 18 个去重链接，但这些链接没有写入 SQLite/result artifact。
12. **真实跨账号候选**：**没有形成**。原 `cli_failed` 已定位为隔离
    CLI/private-runtime 状态不完整，control 已恢复，第二账号 profile/note 也已
    成功；当前阻塞改为第二账号 shop evidence 缺失。系统没有把进程内18个链接、
    失败任务或测试夹具冒充真实候选。
13. **真实候选引用**：因为没有形成真实跨账号候选，所以没有可列的真实机会
    支撑引用。现有可读基线是 `real-account-A` 的一个 trusted shop `artifact:*`
    和 62 个 `account-note:*`；具体私人账号 ID、来源链接和 runtime 路径不写入 Git。
14. **测试结果**：analysis+content focused `458 passed, 1 skipped`；media
    compatibility `57 passed`；frontend `8 files / 53 passed`；production build
    passed；受控双账号 Playwright E2E `1 passed`。完整 backend 首轮为
    `1396 passed, 3 skipped, 1` 个无关并发 5 秒墙钟波动；该精确用例随后连续
    5/5 通过；最终完整复跑为 `1397 passed, 3 skipped`（3 项均为显式 live
    gates），无失败。
15. **真实 UAT**：身份保持的 Stage 2 隔离副本继续证明 `real-account-A` 的
    1 profile、62 notes 和 complete 2/2/2 shop；真实千帆 8/8 通过。control
    恢复后，第二账号成功持久化 1 profile + 10 notes。其 shop discovery job 持久
    保存 66 screenshots + 66 UI hierarchies，但保存 0 个 result artifact 和 0 个
    reusable source URL；AI 分析未调用。详见 `docs/PHASE_A_UAT_REPORT.md`。
16. **未解决问题和限制**：第二账号的18个商品链接只存在于当时进程内，未形成
    `shop_collection_result`、`collection.json`、图片 manifest 或 SQLite 商品事实。
    当前样本是 `0/3`，不是 `3/3`。真实 Phase A 尚未通过；没有真实两账号聚类、
    pending_review 或人工审核结果；Phase B 未启动，七天 UAT 未运行。

## Phase A 关键修改文件

- `SYSTEM_SPEC_AND_ACCEPTANCE.md`：把跨账号需求验证、服务端等级和人工审核写入
  正式验收线。
- `README.md`：更新当前 Phase A 操作边界。
- `docs/superpowers/specs/2026-08-20-cross-account-demand-validation-design.md`：
  记录批准的业务/数据/API/UI/UAT 设计。
- `backend/app/db.py`：Phase A 机会字段、版本化迁移、历史降级和审核/证据触发器。
- `backend/app/features/analysis/models.py`：持久化审核、等级和支撑证据字段。
- `backend/app/features/analysis/schemas.py`：跨账号输入、逐账号模型输出和审核 schema。
- `backend/app/features/analysis/service.py`：逐账号 trust/ownership、服务端等级、候选
  投影、审核状态机和 commit-ack 图验证。
- `backend/app/features/analysis/api.py`：机会审核 API。
- `backend/app/features/radar/models.py`：缺失评分状态、出现次数和最佳名次。
- `backend/app/features/radar/service.py`：无指标候选保留和稳定事实排序。
- `backend/app/features/content/schemas.py`：允许可信 `account-note:*` 引用。
- `backend/app/features/content/service.py`：approved 跨账号机会和 account-note/shop
  再核验门禁。
- `backend/app/adapters/xhs_cli_readonly_wrapper.py`：真实 CLI shape、只读滚动和有界
  latest account-note 读取。
- `backend/app/adapters/android_device.py`：固定栏排除和相邻 viewport 稳定去重。
- `backend/app/features/shops/scope.py`：最多三件代表商品的店铺类型前置门。
- `backend/app/features/shops/service.py`：bounded sample、sample/shop 完整性分离和
  manifest/collection SHA 绑定。
- `frontend/src/api/client.ts`：Phase A 账号/机会/审核 API 类型与请求。
- `frontend/src/pages/AccountPage.tsx`：只显示账号报告/观察信号。
- `frontend/src/pages/OpportunitiesPage.tsx`：多账号完整度、聚类、证据和审核工作台。
- `frontend/src/pages/RadarPage.tsx`：`insufficient_metrics` 事实展示。
- `frontend/e2e/fixture_app.py`：受控双账号真实 schema/provenance fixture。
- `frontend/e2e/empty-to-package.spec.ts`：双账号聚类、warming、人工批准及既有回归链。
- `docs/PHASE_A_UAT_REPORT.md`：隔离真实 UAT 的事实与阻塞。
- `docs/UAT_CHECKLIST.md`：Phase A live gate 最新结果。
- `PROJECT_STATUS.md`：本交接报告。

## Phase A Git 提交

- `44725777df75d647a91c2ccb66b2913b4eb1b929` —
  `docs: define cross-account validation phase`：同步正式规格、设计和阶段边界。
- `c6cf45089989447a041c08a8496b9726c3665e9c` —
  `feat: validate cross-account opportunities`：实现数据库、API、服务、前端和受控
  双账号 E2E。
- `980bc6636467e24aa1a70bd50b5adf9d25cdb497` —
  `fix: bound latest xhs account samples`：账号主页默认保存 latest-10
  工程样本，不冒充全量。
- `d53c97a18fa03584234b33ffc1347b42b5eae3bf` —
  `fix: deduplicate overlapping shop viewports`：修复 Android 重叠卡片
  与动态短链重复计数。
- `d8f25b2006d6ddb557d1cd21b288cfe7300cbd0f` —
  `feat: gate shop collection with bounded samples`：店铺类型前置门、
  当前测试店3件样本语义和前端展示。
- `4f164505dcb455bdd4cdc68c4693fc5a37a78e59` —
  `fix: bind bounded shop sample evidence`：绑定 sample manifest、
  collection SHA，并为分析增加严格3/3信任门。
- `feature/system-v1` 的交付 HEAD —
  `docs: record Phase A real UAT handoff`：记录真实 UAT、最终测试和交接状态。
  Git commit 不能在自己的文件内容中自引用其最终 SHA；该提交的完整 SHA 以
  `git log -1`、远端 `refs/heads/feature/system-v1` 和最终交付回报三方核对。

## Phase A 交付状态

- 稳定软件改动：已提交；不等同于真实 UAT 完成。
- 真实跨账号 UAT：未完成；control 与第二账号 profile/note 已恢复，当前阻塞于
  第二账号18个商品链接未持久化，可信商品样本 `0/3`。
- Phase B：未开始。
- GitHub：第三笔提交创建后推送并核对本地/远端 SHA；结果由最终交付回报确认。

## Phase A Drift Correction

- **保留的真实能力**：千帆 8 榜、XHS CLI/session 恢复、`real-account-A` 的可信
  profile/历史 62 篇笔记/商品 2/2/2、第二账号的可信 profile/latest-10 笔记、
  Android 重叠卡片去重、原始截图/UI hierarchy、SQLite/jobs/evidence/hash、跨账号
  数量与审核门禁。
- **已收回的支线**：不再为服装测试店增加商业验收特例，不再用工程测试数量替代
  Phase A 真实闭环；Phase B、PersonalOS、UI 重做及无关加固均不属于当前主线。
- **test override 隔离**：`bounded_sample/test_override` 只保留测试、debug 和受控
  预检价值；即使样本文件完整，也不得成为真实 Opportunity evidence。
- **PersonalOS 降级**：`personal-os/` 保留历史，但已明确标记为
  `NOT AUTHORITATIVE FOR XHS WORKBENCH`；正式规格以
  `SYSTEM_SPEC_AND_ACCEPTANCE.md` 为最高优先级。
- **当前实体服装账号**：保留其 profile、latest-10 笔记、132 份 Android 原始证据
  和历史 `needs_human` 任务；业务分类为 `out_of_scope_physical`，退出真实 Phase A
  第二账号候选，不改写历史失败。
- **discovery persistence**：当前仍是待关闭的直接阻塞。历史观察到的 18 个链接未
  形成结构化 result、source URL artifact 或 SQLite 商品绑定，可信商品仍为 0；禁止
  把它们补写成成功或重新扫描该服装店掩盖缺口。
- **Phase A 当前状态**：真实跨账号分析未运行，candidate/pending_review/人工审核均
  为 0；Phase A real UAT 仍未通过，Phase B 未开始。
