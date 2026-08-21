# START HERE — ChatGPT / Codex 项目入口

新会话或新 Agent 进入本仓库时，按以下顺序读取：

1. `docs/AI_HANDOFF_CURRENT.md` — 当前真实状态和下一步。
2. `docs/CODEX_NEXT_OBJECTIVE.md` — A/B/C/D 四段架构、两个断点和 Codex 边界。
3. `docs/REFERENCE_MAP.md` — 原教程、需求雷达分享版、内容系统分享版与当前工程的关系。

只在当前任务需要时再读具体代码、UAT 或参考资料，不要一开始全仓扫描。

## 当前架构

```text
A 需求雷达 / Opportunity discovery
  ↓
B 产品研究 / 产品定义（当前先独立人机协作）
  ↓
人工批准具体 Product Definition
  ↓
C 产品制作（独立 AI / Codex 工作段）
  ↓
Finished Product + 人工 UAT
  ↓
D 内容系统（产品完成后才接入）
```

A 回答“什么值得继续研究”；B 回答“具体做什么产品”；C 把批准的产品定义做成真实成品；D 围绕 Finished Product 做内容研究与持续生产。

## 当前真实案例

Opportunity：`七宗罪心理测试数字内容市场机会`

- Opportunity ID: `caed5776-ef38-4a0a-90fe-57ae2291583e`
- analysis ID: `5859e6c9-0485-451b-9152-05896c0d037b`
- Phase A 已形成两账号证据闭环和成功跨账号 analysis。
- 用户已明确批准该 Opportunity 继续进入产品研究。
- 最后一次核验本地持久化状态仍为 `warming_candidate + pending_review`；如 DB/API 尚未记录该人工批准，应走现有人工审核路径持久化，不改写历史 evidence/analysis。

该批准仅表示“值得继续研究”，不等于已经决定具体产品形态，也不等于已经开始产品制作或内容系统。

## 当前下一步

以当前 Opportunity 作为第一个真实案例，单独跑一次产品研究 / 产品定义：

1. 读取 Phase A Opportunity 和证据；
2. 列出已知与未知；
3. 必要时取得合法的真实竞品交付样本；
4. 分析竞品交付物、结构、用户、体验、同质化、缺口和风险；
5. 形成一个或多个 Product Definition 候选；
6. 用户做第二次决定：批准具体产品立项、继续研究或放弃。

只有具体 Product Definition 获批后，才进入独立产品制作工作段。

## 参考资料入口

- `references/original-tutorial/406亿Token教程/教程正文整理版.md`
- `references/original-tutorial/需求雷达-分享版/REFERENCE_DIGEST.md`
- `references/original-tutorial/内容系统-分享版/REFERENCE_DIGEST.md`
- `references/original-tutorial/SOURCE_INVENTORY.md`

## 优先级

`当前 handoff / 当前代码 / 当前 UAT` 高于 `教程正文`，教程正文高于 `分享版参考实现`。

教程用于理解“作者怎么做”；当前工程用于回答“我们现在实际上怎么做”。
