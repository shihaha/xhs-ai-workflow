# 需求雷达-分享版 — Reference Digest

> 来源：用户上传的 `需求雷达-分享版(3).zip`。本文件用于让 ChatGPT / Codex 在不重新上传 ZIP 的情况下理解分享版结构和关键实现。它不是当前 `xhs-ai-workflow` 的生产规范。
>
> 原 ZIP：128,060 bytes；SHA-256：`0536e14bdca09de1ecdecb2cbf8d360143e5abad81030c3185cdda3c608019e0`
> ZIP 总条目：115；解压后普通文件：74。

## 1. 分享版定位

分享版 README 的原始定位是：

> 本地账号深挖工具：从千帆 8 个榜单筛选账号，采集商品详情和商品图，汇总机会结论并展示在 Obsidian 面板。

它的目标不是自动做产品，而是从榜单事实里筛出值得继续判断的机会，再把证据摆给人。

## 2. 总体链路

`01-系统设计/架构总览.md` 给出的主链路：

```text
千帆 8 个榜单
        ↓
榜单原始事实（SQLite / data/raw）
        ↓
账号评分（score_accounts.py）
        ↓
逐账号采集商品详情和本地商品图
手机拿链接 + 本机配置指定的固定 Ego 空间
        ↓
N/N 核验
唯一商品链接数 = 唯一 detail.json 数，且图片齐全
        ↓
逐账号图文分析（analysis.json）
        ↓
跨账号汇总（data/deep-dive/YYYY-MM-DD.json）
        ↓
deep_dive_sync.py
        ↓
SQLite / 需求卡 / Obsidian 面板
```

## 3. AI / 人的分工

分享版明确写的是：

- AI：按工作流规则完成采集、账号评分、商品证据、账号分析、跨账号汇总和面板同步；
- 人：在面板查看商品、判断是否继续推进，并把决定写进需求卡。

因此分享版本身就是“AI 跑证据，人做业务选择题”的系统。

## 4. 机会准入规则

`01-系统设计/需求认证规则.md` 的核心约束：

- 先过采集关：唯一商品链接数与唯一 `detail.json` 数相等，且本地商品图全部检查后，账号才可以进入机会判断；
- 结论看多个已深挖账号是否出现相近商品形态、价格带或交付方式；
- 商品详情和商品图要能互相印证卖点、交付内容和实际页面；
- 要有可见销量、价格和账号表现支撑，不能靠猜；
- 低粉高销、数字化交付或标准化交付会提高“可复制性”判断；
- 图片、详情、价格或销量无法核实时降级；依赖侵权/灰产/不适合交付时放弃。

面板状态原版包括：`观察中 / 升温 / 已验证 / 降温 / 放弃`。

## 5. 工作流总规则

`AI协作/工作流总规则.md` 把一次完整运行固定为：

```text
千帆 8 榜采集
→ 账号评分
→ 逐账号商品链接与详情图片采集
→ N/N、图片哈希、图文分析核验
→ 跨账号汇总
→ 同步数据库、需求卡和 Obsidian 面板
```

关键硬边界：

- 8 个榜单入口未完成前不得开始评分；
- 每个候选账号通过手机采集脚本逐个处理；
- 每个通过采集的账号必须有 `collection.json`、全部 `detail.json`、本地 `images/`、`analysis.json`；
- 主代理不能凭自身模型能力“假定图片已分析”，要派账号图文分析子代理逐张查看；
- `visual_evidence.inspected_image_count` 必须等于本地图片总数，`image_checks` 必须覆盖每张图；
- 图片未完整查看时账号不得进入汇总；
- 先跑 `verify_deep_dive.py`，再跑 `deep_dive_sync.py apply`。

## 6. 账号图文分析子代理

`AI协作/子代理提示词/账号图文分析子代理.md` 的职责是一账号一分析：

- 读取 `collection.json`；
- 读取每个商品的 `detail.json`、`images/` 和图片哈希；
- 逐张打开全部本地商品图；
- 每张图写真实观察，不用标题/详情文字替代图像结论；
- 必须记录图片文件路径、验证方式、SHA-256、真实观察；
- 不能跨账号比较、不能写需求卡、不能写跨账号汇总。

输出的 `analysis.json` 包含：账号主品类、价格范围、top product、总商品数、产品列表，以及覆盖每个商品和每张图的 `visual_evidence`。

## 7. 主要脚本与目录

关键脚本：

- `scripts/pipeline/score_accounts.py` — 从榜单事实中评分候选账号；
- `scripts/collect/qianfan-note-rank.sh` — 千帆榜单采集；
- `scripts/collect/phone-collector/collect-account.sh` — 手机逐账号进入店铺并采商品；
- `scripts/collect/phone-collector/xhs-common.sh` — 手机端公共操作；
- `scripts/pipeline/verify_deep_dive.py` — 深挖证据核验；
- `scripts/pipeline/deep_dive_sync.py` — 汇总同步唯一入口；
- `scripts/db/schema.sql` — SQLite 结构；
- `scripts/dashboard/export_dashboard.py` / `install_plugin.py` — Obsidian 面板。

关键数据目录：

```text
data/account-deep-dive/<日期>/<账号>/<商品>/detail.json
data/account-deep-dive/<日期>/<账号>/<商品>/images/
data/account-deep-dive/<日期>/<账号>/analysis.json
data/deep-dive/<日期>.json
```

数据库表在分享版 `docs/DATA_SCHEMA.md` 中包括：

- `collection_runs`
- `raw_rank_items`
- `deep_dive_runs`
- `deep_dive_accounts`
- `deep_dive_demands`
- `knowledge_documents`

## 8. Obsidian / Claude 适配

分享版同时提供：

- `.obsidian/plugins/demand-radar-dashboard/` 与 `obsidian-plugins/demand-radar-dashboard/`；
- `.claude/agents/account-analyst.md`；
- `.claude/agents/qianfan-collector.md`；
- `.claude/skills/xhs-deep-dive/SKILL.md`；
- `workflows/deep-dive.md`；
- `workflows/claude-daily-schedule.md`；
- `AI协作/AI启动说明.md`。

`AI启动说明.md` 要求 AI 启动前依次读取 `PROJECT_CONTRACT`、`DATA_SCHEMA`、工作流总规则、千帆采集子代理、账号图文分析子代理。

## 9. “产品实验”目录意味着什么

ZIP 里存在：

```text
05-产品实验/
  候选/
  进行中/
  已完成/
```

但这些目录在分享包中是空目录，没有一套对应的完整“产品研究 Agent / 产品制作 Agent / 自动状态机”。所以不能把它解释成已经存在一个完整的 Phase B 产品研究系统。

分享版真正工程化完整的部分仍然是：**从榜单到账号深挖、跨账号机会、面板给人判断。**

## 10. 与当前 `xhs-ai-workflow` 的关系

当前仓库不是简单复制此分享版。当前工程已经增加/强化了：

- FastAPI + React 工作台；
- SQLite 持久化业务状态；
- Job / Artifact / Evidence；
- SHA / manifest / evidence trust；
- fail-closed；
- 当前布局/选择器真实性门；
- 跨账号 specific shared demand 契约；
- Opportunity `pending_review` 人工审核门。

因此以后引用分享版时，回答“原作者怎么做”；修改当前工程时，以 `docs/AI_HANDOFF_CURRENT.md`、当前代码和 UAT 为准。
