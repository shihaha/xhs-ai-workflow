# 小红书需求雷达与内容生产系统：规格与验收基准

版本：V2
日期：2026-08-20
状态：Phase A 已批准实施；Phase B–E 仅定义业务边界

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

统一接口覆盖榜单、搜索、笔记、账号与商品。实现优先顺序：教程附件可复用代码、`xiaohongshu-mcp`、`xhs-cli`、`MediaCrawler`、自有 Playwright 适配器。具体实现只能通过适配器边界接入，业务层不依赖第三方项目的数据结构。

### 3.3 安卓设备适配器

使用 ADB、adbutils 与 uiautomator2。提供连接检测、前台应用检测、导航、截图、UI 层级保存、商品遍历、暂停及人工接管。验证码、登录失效、页面结构变化和设备断开必须转入 `needs_human` 或 `failed` 并留下证据。

### 3.4 需求雷达

保存千帆八榜的原始快照和标准化条目；对有完整评分字段的账号进行可配置、可解释的评分；字段不足时必须明确显示 `insufficient_metrics`，不得用零分冒充真实评分。候选账号继续完成公开资料、笔记、店铺商品、商品图片和 N/N 证据采集。

单账号分析只形成观察信号。跨账号商品/需求聚类必须覆盖至少两个不同的 `account_user_id`，且每个支撑账号都必须有可信账号笔记和完整 shop artifact。商品与图片通过 shop artifact、manifest、SHA-256 和来源链接追溯。

证据等级是本系统规则，不是平台官方结论：一个账号为观察信号且不得生成机会；两个账号为 `warming_candidate`；三个及以上不同账号为 `validated_candidate`。等级只由服务端根据经验证证据计算，模型和人工均不得直接改写。

AI 聚类通过严格 schema 和证据归属校验后，候选机会立即以 `pending_review` 持久化。人工只能将其单向审核为 `approved` 或带原因的 `rejected`；拒绝记录不得删除。只有 `approved` 且证据合格的机会才能进入产品方向选择。

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

- 从真实千帆候选池选择至少两个不同账号，并分别完成公开资料、笔记和商品 N/N 可信采集。
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
