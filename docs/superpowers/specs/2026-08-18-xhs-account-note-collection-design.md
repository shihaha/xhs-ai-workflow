# 小红书账号与笔记只读采集补漏设计

## 1. 背景与目标

`SYSTEM_SPEC_AND_ACCEPTANCE.md` 已要求统一采集接口覆盖榜单、笔记搜索、账号和商品，但当前生产应用只接通了千帆榜单与安卓商品核验。`search_notes`、`fetch_account` 仍停留在协议声明，账号分析没有真实账号笔记证据链。

本设计补齐以下闭环：

`榜单账号 → 账号资料/账号笔记只读采集 → 原始证据持久化 → 账号页展示 → AI 分析引用笔记证据`

商品仍由现有安卓真机适配器负责，不创建第二套商品采集。

## 2. 选型

V1 使用已审计并已下载的 `xhs-cli` 作为首个生产只读适配器，原因是它在 Windows 上提供稳定的 JSON 命令边界，覆盖 `search`、`read`、`user` 和 `user-posts`，接入成本低于维护一个常驻 MCP 服务。

`xiaohongshu-mcp` 保留为后续可替换适配器，不在本次同时接入。MediaCrawler 不作为 V1 主路径，避免增加浏览器签名与运行生命周期的第二套实现。

业务层只依赖现有 `CollectorAdapter` 规范，不依赖 `xhs-cli` 原生 JSON 结构。

## 3. 安全边界

- 适配器只允许固定命令：`status`、`whoami`、`search`、`read`、`user`、`user-posts`。
- 明确禁止登录、退出、发布、点赞、收藏、评论、关注和私信命令。
- HTTP 请求不能传入可执行文件路径、任意子命令、Shell 片段、Cookie、URL 或环境变量。
- `xhs-cli` 可执行入口和状态目录只来自受信 `Settings`。
- 通过参数数组启动子进程，禁止 Shell 字符串拼接。
- Cookie、token、请求头和用户隐私字段不进入数据库、日志、API 响应或证据元数据。
- 未登录、Cookie 失效、验证码、风控、限流和账号不可见统一形成可审计 `needs_human`，不能写成功数据。
- 不绕过平台限制，不执行平台写操作。

## 4. 适配器与任务

新增 `XhsCliReadAdapter`，实现：

- `search_notes(CollectionRequest)`：按严格关键词搜索公开笔记。
- `fetch_account(CollectionRequest)`：读取一个内部 `user_id` 的账号资料及账号笔记列表。
- `fetch_products` 不实现，由 AdapterRegistry 将该能力解析到现有 Android 设备服务。

新增两类保留任务：

- `xhs_note_search`
- `xhs_account_collection`

任务状态沿用 `queued/running/needs_human/succeeded/failed/cancelled`。通用 Jobs API 不得 claim、伪造日志、上传专用证据或把这些任务转成成功；只保留受控取消。

每个采集任务使用有界后台 worker。进程关闭后不接收新任务；已进入外部命令的任务在返回后检查取消栅栏，不能晚到写入成功。

## 5. 数据与证据

新增持久表：

### `xhs_account_profiles`

- `user_id`：平台内部账号 ID，唯一。
- `nickname`、`bio`、可用的公开统计字段。
- `source_url`。
- `collection_job_id`、`artifact_id`、`collected_at`。
- `raw_digest`：规范化原始响应 SHA-256。

### `xhs_account_notes`

- `note_id`：平台笔记 ID。
- `user_id`：归属账号。
- `title`、公开摘要、发布时间和公开互动字段。
- `source_url`。
- `collection_job_id`、`artifact_id`、`collected_at`。
- `raw_digest`。
- `(note_id, user_id)` 唯一，并验证账号归属。

### 原始证据

每次外部命令的原始 JSON 先写到受管证据目录，再以 `JobArtifactRecord` 绑定：

- job ID、能力、账号 ID 或关键词。
- 适配器名与版本。
- 命令白名单标识，不保存命令行敏感值。
- 原始文件相对路径、SHA-256、大小和采集时间。
- N/N 计数、缺失与拒绝事实。

只有 `CollectionResult` 为 exact complete 时才在同一数据库事务中写账号/笔记事实并把 job 转为成功。partial、rejected、unknown-total 和 `needs_human` 只保存证据与状态，不升级为成功账号笔记。

## 6. API

- `POST /api/v1/accounts/{user_id}/collections`
  - body 只接受严格 `expected_note_count` 上限。
  - 返回 HTTP 202、job ID。
- `POST /api/v1/notes/search-collections`
  - body 只接受规范化关键词和严格数量上限。
  - 返回 HTTP 202、job ID。
- `GET /api/v1/accounts/{user_id}/profile`
- `GET /api/v1/accounts/{user_id}/notes`
- `GET /api/v1/note-search-results?job_id=...`

任务详情与取消继续复用受控 Jobs API。读取 API 只返回规范化公开字段和证据 ID，不返回原始 Cookie/token。

## 7. 分析接入

- `analysis-evidence` 发现端点增加可信账号笔记证据。
- 账号分析请求引用笔记时，服务端重新验证 job 类型、producer、artifact 路径/hash、账号归属与 N/N 完整性。
- 模型输出中的 evidence ID 必须属于当前分析请求允许集合。
- 没有完整账号笔记证据时可以分析现有榜单/商品事实，但必须明确标记证据缺口；不能声称已经完成笔记深采。

## 8. 前端

账号页增加：

- “采集账号与笔记”按钮，single-flight 防重复。
- 真实 job 状态、N/N、失败详情和人工处理提示。
- 账号资料与笔记列表。
- 笔记证据 ID 和来源链接。
- 重新采集会创建新 job，旧 job 保留审计。

雷达页增加关键词笔记搜索入口，搜索结果与账号采集结果明确区分。

## 9. 测试与验收

### 自动测试

- 子进程命令白名单、参数注入、超时、输出上限、非 JSON、敏感字段净化。
- exact N/N、重复笔记、跨账号归属、原始证据 hash 和事务原子性。
- 通用 Jobs API 不能伪造专用任务/证据。
- 登录失效、验证码、限流和命令不可用进入真实状态。
- 分析只能引用当前账号的可信笔记证据。
- 前端 empty/loading/error/needs_human/succeeded 及防双击。
- 受控 E2E：榜单账号 → 账号资料/笔记 → 分析机会，不再跳过账号笔记采集。

### Live 验收

用户在本机完成 `xhs-cli` 登录，不向应用或聊天提供密码/Cookie。至少用一个真实账号和一个关键词执行采集，核对来源页面、N/N、证据文件和数据库一致性。未完成前状态必须为 `not_run`。

## 10. 非目标

- 不自动登录或代用户保存密码。
- 不发布、点赞、评论、收藏、关注或私信。
- 不在本轮同时接入三个第三方仓库。
- 不承诺绕过验证码、风控或平台页面限制。

