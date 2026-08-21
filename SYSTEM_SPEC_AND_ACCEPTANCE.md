# 小红书需求雷达与内容生产系统：规格与验收基准

版本：V2.1
日期：2026-08-21
状态：Phase A 已批准实施；Phase B–E 仅定义业务边界

## 2026-08-21 当前验收状态

- 当前 71 位真实千帆候选已按原始评分顺序处理完毕；第 62～71 位均为
  `uncertain -> needs_human`，没有新增 `in_scope`，没有人工选品类或配对。
- 当前候选池有 2 个具备 profile、latest-10 和 clean shop evidence 的
  Opportunity-eligible `in_scope` 账号。它们的正式 2/2 evidence jobs 分别为
  `8507e249…` 与 `fb293aa0…`，全部 SHA-bearing artifacts 当前字节一致。
- clean-only 统一分析 `5d4e235a…` 使用 2 个账号、22 条证据，但百炼最终
  `model_retry_exhausted`。因此本轮没有可批准的最新 Opportunity，Phase B 不得启动。
- 旧 Windows newline XML mismatch 正式分类为
  `legacy_historical_audit_limitation`：历史 jobs/artifacts 永久保留且不改写；当前 XML
  生产路径按实际写盘 bytes 计算 SHA，当前 eligible evidence 不依赖旧 mismatch，故该
  历史限制不再是 Phase A 当前 blocker。
- 一个较早分析曾基于后来失去 eligibility 的旧 shop evidence 生成 pending candidate；
  它保留作历史审计，批准时必须重新验证当前证据信任，不能据此进入 Phase B。

## 0. 规格优先级与防跑偏协议

本文件是小红书工作台的最高优先级业务规格。其后依次为
`PROJECT_STATUS.md`、`docs/PHASE_A_UAT_REPORT.md`、
`docs/UAT_CHECKLIST.md`、`docs/IMPLEMENTATION_STATUS.md`。历史 SDD 只作技术参考；
`personal-os/` 不是本工作台的业务事实源。

当前唯一交付物是 Phase A 真实跨账号需求验证。业务交付优先于工程完善；仅修复
直接阻塞当前交付的技术缺陷。涉及采样数量、代表性、证据资格、机会门槛或 Phase
边界的新业务规则，必须先由用户批准，不能由实现自行增加。每完成 profile、笔记、
商品发现、商品详情或分析一步，都必须立即核验 SQLite、artifact、文件与 SHA，不能
把进程内结果当成已完成。

## 1. 目标

在 Windows 本机运行一套独立 Web 工作台，用真实数据完成以下闭环：千帆八榜采集、候选账号、账号商品/图片/笔记、单账号画像、跨账号需求验证、人工机会审核、人工产品方向选择、真实产品建设、内容研究、模板批准、内容生成、独立审核及待发布内容包导出。

目标是与教程的业务效果等价，不复制教程软件外观。任何进度、状态、数字和产物必须来自数据库中的真实记录及可追溯证据，禁止演示数据冒充真实结果。

## 2. 固定边界

- 代码目录：`D:\AI_WORKSPACE\xhs-intelligence-workbench`。
- 运行数据目录：`D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench`，不得进入 Git。
- 教程 HTML 与附件是只读来源；允许复制、修改和集成有技术价值的代码。
- V1 单机、单用户、单小红书账号、单安卓设备。
- V1 只生成待发布内容包，不自动发布、点赞、收藏或评论。
- 采集必须受控批量执行，提供限速、暂停、取消、人工接管和证据保留。
- 语言模型只通过阿里云百炼调用；无 API Key 时系统必须明确显示“未配置”，不得伪造结果。
- SQLite 为唯一业务数据库；不要求安装数据库服务器。

## 3. 系统模块

### 3.1 任务与证据底座

任务状态固定为 `queued`、`running`、`needs_human`、`succeeded`、`failed`、`cancelled`。每个任务保存类型、输入、进度计数、当前阶段、错误分类、重试次数、时间戳、日志及证据文件。任务崩溃后重启，遗留的 `running` 必须变为 `needs_human`，不得自动显示成功。

### 3.2 采集适配器

统一接口覆盖榜单、搜索、笔记、账号与商品。Windows 账号资料和主页公开笔记的当前主读取路径固定为：独立持久、可见的 Chrome collection profile，经人工正常登录后，通过仅限 localhost 的 CDP 复用该浏览器上下文；不得自动登录、不得回退 QR，也不得把 CDP endpoint 放入普通 API 请求。`xhs-cli` 代码保留为显式备用实现，关键词搜索暂仍由其适配器提供。具体实现只能通过适配器边界接入，业务层不依赖浏览器页面或第三方项目的数据结构。

### 3.3 安卓设备适配器

使用 ADB、adbutils 与 uiautomator2。提供连接检测、前台应用检测、导航、截图、UI 层级保存、商品遍历、暂停及人工接管。验证码、登录失效、页面结构变化和设备断开必须转入 `needs_human` 或 `failed` 并留下证据。

每发现一个唯一商品身份，必须在后续商品详情/图片深采前持久化结构化 discovery
result，包括 job、账号、稳定身份、可用来源链接、可见元数据、发现顺序、时间、原始
证据引用、result SHA 和 SQLite artifact 绑定。后续失败、取消、设备断开或进程重启
不得抹掉已经持久化的发现结果；截图和 UI hierarchy 不能替代结构化 discovery。

### 3.4 需求雷达

保存千帆八榜的原始快照和标准化条目；对有完整评分字段的账号进行可配置、可解释的评分；字段不足时必须明确显示 `insufficient_metrics`，不得用零分冒充真实评分。候选账号继续完成公开资料、最新 10 篇唯一公开笔记样本、店铺商品、商品图片和证据采集。账号主页笔记是有界分析样本，不做账号全部历史笔记 N/N；严格全量 N/N 只用于边界明确的店铺商品、商品图片、manifest 和哈希。

Phase A 候选漏斗必须严格区分八层：①教程原有账号评分；②按 Top N 思想优先处理高分账号，但不把 20 当成候选池硬上限；③本项目新增的低成本数字/虚拟业务范围预筛；④Android 店铺最终范围判定；⑤排除或失败后按原评分顺序从第 21、22 名继续补位；⑥单店默认顺序连续遇到的前 3 个不同商品代表样本；⑦对所有合格账号统一执行 specific shared demand 分析；⑧人工审核 Opportunity。任何一层不得冒充下一层已完成。

低成本预筛只能使用已经持久化、无需 Android 深挖的公开事实，并持久化账号身份、时间、理由、事实引用、原始摘要哈希及结果 artifact/hash。结果固定为 `likely_digital | clearly_physical | uncertain`：只有明确实体商品和物流/交付事实才可提前跳过；证据冲突、不足、规则异常或引用无效必须 fail-open 为 `uncertain`。`likely_digital` 仍必须执行 Android `preflight`，不得直接成为 `in_scope`、shop evidence 或 Opportunity 证据。已有 Android 结果始终比预筛权威。

候选推进只允许按原评分稳定顺序选择下一名，不接收品类、方向或配对目标。高排名候选的最终实体、`needs_human` 或真实采集失败不会截断池子；系统继续读取下一名，直到获得足够合格账号或当前真实候选池耗尽。不得因为已经发现某个需求方向而定向寻找第二个同类账号。

账号采集的 `sample_limit` 默认值和硬上限均为 10；平台实际可见不足 10 篇时保存实际全部 N 篇并标记 `sample_exhausted`，达到 10 篇时标记 `bounded_sample`，两者均不得表述为账号全量完成。已持久化的旧 62 篇记录继续作为历史证据保留，但新账号分析默认只消费当前任务产生的最新 10 篇样本。关键词对标采集与账号主页采样相互独立：教程业务口径为每个实际搜索词目标 5–10 篇达标笔记，首轮先采 2 篇做完整性测试，同产品跨关键词去重，达到目标立即停止。

单账号分析只形成观察信号。跨账号商品/需求聚类必须覆盖至少两个不同的 `account_user_id`，且每个支撑账号都必须有可信账号笔记和完整 shop artifact。商品与图片通过 shop artifact、manifest、SHA-256 和来源链接追溯。跨账号分析必须先分别总结每个账号的主要售卖内容、目标用户、核心购买动机或问题、产品/交付形态及使用场景，再判断这些账号是否共同证明同一个足够具体、可执行的市场需求。共同销售渠道、共同营销动作，或“省钱、赚钱、学习、效率、变美”等宽泛上位需求，均不能单独构成 Opportunity；没有可信共同具体需求时必须持久化判断理由并返回 `opportunities=[]`。

店铺采集分为四种业务语义：`preflight` 只用少量真实商品判断
`in_scope/out_of_scope_physical/needs_human`，永远不能成为 Opportunity 证据；
`full_shop` 只用于规模较小或确需完整画像的店铺；经用户批准的正式
`evidence_sample` 固定采集店铺默认排序下连续遇到的前 3 个不同商品，不得挑选、
跳过或用第 4 件替换失败项。三件均须绑定详情页、稳定来源身份、真实图片、
manifest、SHA-256、SQLite 和同一任务证据，且必须已有可信 `in_scope` preflight；
成功时写 `sample_complete=true`、`shop_complete=false`，可以参与 Phase A 跨账号
需求验证。现有 `bounded_sample/test_override` 仍只属于测试、debug 或受控预检，
必须保持 `shop_complete=false`，且永远不能成为真实 Opportunity 证据。

证据等级是本系统规则，不是平台官方结论：一个账号为观察信号且不得生成机会；两个账号为 `warming_candidate`；三个及以上不同账号为 `validated_candidate`。等级只由服务端根据经验证证据计算，模型和人工均不得直接改写。

AI 聚类通过严格 schema、共同具体需求合同和证据归属校验后，候选机会立即以 `pending_review` 持久化；最终共同需求结论自身必须引用每个请求账号的证据。人工只能将其单向审核为 `approved` 或带原因的 `rejected`；没有正向共同具体需求结论的历史或负向分析候选只能拒绝，不能批准，拒绝记录不得删除。审核页必须展示每个账号的需求画像、共同需求、共同点、关键差异、形成或不形成候选的理由及支撑证据。只有 `approved` 且证据合格的机会才能进入产品方向选择。

### 3.5 AI 适配器

文本分析默认使用百炼中的 DeepSeek 文本模型；图片理解与图片生成使用通义视觉/万相兼容模型。所有请求保存模型、提示词版本、输入证据引用、结构化输出、用量、耗时和错误。结构化输出必须通过 Pydantic 校验，失败可重试但不能写入成功结果。

### 3.6 产品与内容工作流

Phase A 只完成跨账号验证和机会人工审核，不开始产品建设。现有 `content_products` 在 Phase B 前只视为历史产品档案，不代表产品已经完成。

Phase B 的产品状态依次为 `direction_selected`、`brief_ready`、`building`、`awaiting_review`、`approved`；产品形式、价格、差异化和最终方向必须由人工决定。Phase C–E 才依次处理批准产品分析、关键词/对标笔记研究、模板聚类与批准、Skill 化、内容生成、独立审核和内容包导出。评论、视频、转写、自动发布和自动互动不属于当前范围。

未引用产物只能经持久清理队列转入同卷隔离区，移动后的路径与物理身份必须可追溯；业务引用与隔离记录采用双向数据库约束，任何等价路径冲突、身份或事务结果不明确都保留文件并进入人工处理。

## 4. 公开接口

- `CollectorAdapter.collect_rankings(request) -> CollectionResult`
- `CollectorAdapter.search_notes(request) -> CollectionResult`
- `CollectorAdapter.fetch_account(request) -> CollectionResult`
- `CollectorAdapter.fetch_products(request) -> CollectionResult`
- `DeviceAdapter.health() -> DeviceHealth`
- `DeviceAdapter.collect_shop(request) -> CollectionResult`
- `ModelAdapter.generate_structured(request, schema) -> ModelResult`
- HTTP API 使用 `/api/v1` 前缀；长任务创建返回持久化任务 ID。
- `POST /api/v1/analyses`：单账号报告或至少两个账号的跨账号分析。
- `POST /api/v1/opportunities/{id}/review`：人工批准或带原因否决候选机会。
- `POST /api/v1/radar/candidate-prescreens`：按原评分顺序对真实候选写入低成本范围预筛证据。
- `GET /api/v1/radar/candidate-funnel`：同时展示预筛和 Android 最终判定，不混淆二者。
- `POST /api/v1/radar/candidate-funnel/advance`：只将系统选出的下一名候选送入既有 Android 三商品 `preflight`。

## 5. 验收门槛

### 5.1 自动化验收

- 评分、去重、任务状态转换、结构化模型输出和 N/N 核验有单元测试。
- SQLite、API、适配器契约和内容包导出有集成测试。
- 前端构建通过；关键页面覆盖空状态、运行状态、失败状态及人工接管状态。
- 新数据库启动时不得自动插入演示业务数据。

### 5.2 本机验收

- Web 工作台可启动，健康检查显示数据库、运行目录、ADB、浏览器和百炼配置的真实状态。
- 创建任务后，页面状态与数据库记录、日志和证据一致。
- 缺少 ADB、手机、登录或 API Key 时给出准确的可操作提示。

### 5.3 Phase A 真实验收

- 对真实千帆候选池执行有证据、可重启复读的三态预筛；明显实体可跳过，疑似数字和不确定项必须继续 Android，排除/失败后按原排名补位且不得定向配对。
- 从真实千帆候选池选择至少两个不同且符合数字交付业务范围的账号，并分别完成公开资料、最新 10 篇有界笔记样本（不足 10 篇时为真实 `sample_exhausted`）和经批准规则形成的可信商品证据。
- `out_of_scope_physical` 账号保留历史证据但退出真实候选；`needs_human` 不得自动扩大采集。`preflight` 和任何 `test_override` 均不得参与真实跨账号机会。
- 跨账号聚类必须逐账号引用 shop artifact 和账号笔记；不能用一个账号生成“跨账号机会”。
- 两账号候选必须显示 `warming_candidate + pending_review`；三账号升级规则由自动化测试证明。
- 未批准候选不能创建产品；批准/否决只改变审核决定，不改变证据事实。
- 真实数据没有共同需求时必须返回无候选，不得为了通过验收伪造机会。

### 5.4 后续真实端到端验收

- Phase B–E 经用户另行确认后，才从已批准机会继续“产品方向 → 产品批准 → 内容研究 → 模板批准 → 内容生成 → 独立审核 → 内容包”。
- 商品页宣称发现 N 个商品时，必须保存 N 个成功商品或明确列出缺失项，不能以部分数据冒充 N/N 完成。
- 连续七天受控运行，覆盖 Cookie 失效、设备断开、页面变化、模型限流和任务恢复。
- 未完成上述真实验收前，产品状态只能标记为“软件已实现/待真实验收”，不得宣称达到教程同等效果。

## 6. 非目标

- 多租户、云端 SaaS、移动端管理应用。
- 绕过验证码、风控或平台安全机制。
- 自动发布与自动互动。
- 用虚构数据填充看板或用定时动画模拟任务进度。
