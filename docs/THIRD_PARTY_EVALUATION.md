# 第三方采集候选评估

日期：2026-08-17

本记录只评估将外部代码包在本项目适配器边界后的可替换性。V1 不会调用发布、点赞、收藏、评论或其他互动能力。

## 状态定义

- `unavailable`：本地候选仓库缺失、Git 修订或 `origin` 不可读、`origin` 与官方 GitHub 仓库不匹配，或项目元数据不完整。
- `unverified`：本地源码及其修订可读，但尚未在真实小红书登录态下验证读取能力。
- 本次没有 `verified` 结果。缺少用户提供的登录态时，绝不将候选标记为成功或已验证。

## 本机只读探针

候选以浅克隆方式保存在未纳入 Git 的运行目录：

```powershell
git clone --depth 1 https://github.com/xpzouying/xiaohongshu-mcp.git D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench\third_party\xiaohongshu-mcp
git clone --depth 1 https://github.com/jackwener/xhs-cli.git D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench\third_party\xhs-cli
git clone --depth 1 https://github.com/NanmiCoder/MediaCrawler.git D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench\third_party\MediaCrawler
python tools/probe_xhs_adapter.py --repo-root D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench\third_party
```

探针只运行了 `git rev-parse --short HEAD`、`git remote get-url origin` 并检查项目元数据；来源仅接受无凭据、无 query/fragment、默认端口的 `https://github.com/owner/repository(.git)`，或显式 `git@github.com:owner/repository(.git)` 形式。没有启动候选程序、读取浏览器 Cookie、扫码登录或发起平台请求。

| 候选 | 源地址 | 本机原始证据 | 结果 | 可考虑的只读边界 |
|---|---|---|---|---|
| `xiaohongshu-mcp` | https://github.com/xpzouying/xiaohongshu-mcp.git | `git rev-parse --short HEAD` → `84511f1`; `git remote get-url origin` → `https://github.com/xpzouying/xiaohongshu-mcp.git`; `README.md`、`go.mod` 存在 | `unverified` | 搜索、推荐列表、笔记详情、账号资料；其 README 同时列出发布能力，适配器不得暴露该能力。 |
| `xhs-cli` | https://github.com/jackwener/xhs-cli.git | `git rev-parse --short HEAD` → `3ce7141`; `git remote get-url origin` → `https://github.com/jackwener/xhs-cli.git`; `README.md`、`pyproject.toml` 存在 | `unverified` | `search`、`read`、`user` 等 JSON 输出；README 同时列出互动和发帖命令，适配器必须使用只读 allowlist。 |
| `MediaCrawler` | https://github.com/NanmiCoder/MediaCrawler.git | `git rev-parse --short HEAD` → `d6f7c5b`; `git remote get-url origin` → `https://github.com/NanmiCoder/MediaCrawler.git`; `README.md`、`pyproject.toml` 存在 | `unverified` | 小红书关键词、帖子与创作者读取；README 明示使用浏览器登录态，未提供真实登录前不可验证。 |

## 采用门槛

后续实际接入前，候选必须经由 `CollectionRequest` / `CollectionResult` 返回标准化数据，每条成功项同时保存 source URL 和原始证据。登录失效、验证码、页面变更或缺少执行环境必须返回 `needs_human`、`unavailable` 或 `unverified`，不得报告成功。商品或列表结果只有在 `expected_count == succeeded_count` 时才可标记完成；未成功项必须逐条保留在 `missing_items`。
