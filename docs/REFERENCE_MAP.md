# Reference Map — 原教程、分享版与当前工程的关系

> 这是 ChatGPT / Codex 读取本仓库时的参考资料导航。**不要把参考资料中的旧实现当成当前生产代码。**

## 读取优先级

1. `docs/AI_HANDOFF_CURRENT.md` — 当前真实状态与下一步，最高优先级。
2. `docs/CODEX_NEXT_OBJECTIVE.md` — 当前目标、四段架构和两个断点。
3. 当前仓库生产代码、`docs/RUNBOOK.md`、`docs/UAT_CHECKLIST.md` — 当前真实工程实现与验证记录。
4. `references/original-tutorial/` — 原教程及分享版的参考快照，只用于理解作者方法、比较设计和复用思路。

发生冲突时，**当前工程事实优先于教程/分享版**。不要为了“还原教程”而把已经验证过的当前实现改回旧逻辑。

## 参考资料

### 1. `references/original-tutorial/406亿Token教程/教程正文整理版.md`

由用户保存的原始 SingleFile HTML 抽取出的可检索正文。默认优先读这个版本。

原始上传文件：`大二开公司、从没上过班，我是怎么用406亿Token搭了一套 (2026_8_17 15：56：48)(5).html`

原始 SHA-256：`72040c85e20e0416a5608cb620b3035911b18bd0313ad31e2a58e433a94df039`

这篇文章描述完整方法论：需求发现 → 产品制作 → 内容获客。它不是当前仓库代码规范。

### 2. `references/original-tutorial/需求雷达-分享版/REFERENCE_DIGEST.md`

作者分享的需求发现参考实现的结构化快照。原 ZIP 中包含 Obsidian 看板、脚本、Agent/Skill、账号评分、手机店铺采集、商品证据、账号分析、跨账号汇总等。

原始上传文件：`需求雷达-分享版(3).zip`

原始 SHA-256：`0536e14bdca09de1ecdecb2cbf8d360143e5abad81030c3185cdda3c608019e0`

用途：回答“原作者的需求雷达是怎么实现的？”、对照当前 Phase A 的设计来源。

注意：当前 `xhs-ai-workflow` 已对证据可信度、状态持久化、fail-closed、人工审核等做了自己的工程化改造；不能把分享版直接覆盖回来。

### 3. `references/original-tutorial/内容系统-分享版/REFERENCE_DIGEST.md`

作者分享的**产品完成后的内容研究与生产系统**的结构化快照。典型链路：

`完整产品资料 → 产品分析 → 关键词布局 → 对标采集 → 单篇拆解 → 模板聚类 → Skill → 日更生成 → 内容审查`

原始上传文件：`内容系统-分享版(3).zip`

原始 SHA-256：`1b263bb824de9517dc09fd52cd363bc1f46b0abc87e623ec96bffbd915db42e3`

它不是“产品制作系统”。只有 Finished Product 完成并通过人工 UAT 后，才考虑把产品资料接入这套内容链路。

## 当前统一架构

- **A — 需求雷达**：当前仓库，回答“什么值得继续研究？”
- **B — 产品研究 / 产品定义**：当前先作为独立人机协作工作段，回答“具体做什么产品？”
- **C — 产品制作**：当前先作为独立 AI/Codex 制作工作段，把批准的产品定义做成真实成品。
- **D — 内容系统**：Finished Product 完成后接入，负责持续内容获客。

详细边界见 `docs/CODEX_NEXT_OBJECTIVE.md`。

## 使用规则

- 需要知道“教程原本怎么说”时，优先读 `406亿Token教程/教程正文整理版.md`。
- 需要知道“两个分享版分别负责什么、里面有哪些 Agent/Skill/脚本”时，读对应 `REFERENCE_DIGEST.md` 与 `SOURCE_INVENTORY.md`。
- 参考资料是**来源快照**，不是生产实现；任何修改主系统的动作都要回到当前代码、UAT 和 handoff 核验。
- 不从参考资料中复制 Cookie、Token、API Key 或个人认证状态到生产代码/文档。
- 参考资料来自用户提供的私有材料；仓库应保持 private，除非权利和公开范围已经单独确认。

## 为什么没有把 ZIP 当成主要读取入口

当前 ChatGPT → GitHub 连接器适合写 UTF-8 文本文件，不适合直接把本轮本地二进制 ZIP/SingleFile 原样上传。因此仓库内保存的是：

- 教程正文的可检索整理版；
- 两个 ZIP 的结构化 digest；
- 原始文件名、大小、SHA-256 和完整文件清单；

这样未来 ChatGPT/Codex 可以直接从 GitHub 恢复项目语义和原作者设计。若以后需要把二进制原包也物理归档到 GitHub，可在有本地 `git push`/Codex 文件系统权限时按记录的 SHA-256 补入，不改变这些参考文档的优先级。
