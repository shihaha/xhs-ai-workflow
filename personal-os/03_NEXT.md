# NEXT ACTIONS

> 这里只放“下一步就能直接做”的动作，不放模糊项目名。

## P1｜小红书虚拟产品工作台 V1 收口

### 当前下一步

- [ ] 让 Codex / 当前开发环境先读取 `PROJECT_STATUS.md`、`SYSTEM_SPEC_AND_ACCEPTANCE.md`、`docs/IMPLEMENTATION_STATUS.md`，只列出阻塞 Business-Ready 的未完成项；不重新设计系统。
- [ ] 优先复现并关闭百炼文本 `AnalysisOutput` 的 `model_output_invalid`：完成标准是至少 1 次真实 live analysis 成功写入分析与 Opportunity。
- [ ] 如果文本分析已关闭，下一步立刻做真机商品 2/2 深度证据验证：完成标准是不再停在 `product_evidence_verification_pending`。

### 上面完成以后再做

- [ ] 选择同一个真实榜单账号，串起榜单 → 店铺 → 笔记 → 分析 → Opportunity。
- [ ] 从该 Opportunity 继续跑产品 → 内容 → 图片 → 视觉建议 → 人工审核 → ZIP。
- [ ] 记录 E2E 中出现的阻塞 Bug，只修阻塞项。
- [ ] 再跑一次确认闭环可重复。

### 暂时禁止提前做

- [ ] 不新增自动发布。
- [ ] 不新增矩阵。
- [ ] 不重做 UI。
- [ ] 不为未来可能需求写新模块。
- [ ] 不在真实闭环没通过前继续扩展 Agent / Skill。
- [ ] 不提前进入正式选品业务实验。

---

## Q1｜第一轮真实业务实验

**状态：排队。**

只有 P1 达到 Business-Ready 后才开始：

- Radar 候选约 10 个；
- 深挖 5 个；
- 留 3 个商业假设；
- 选 1 个 MVP；
- 做第一批内容实验；
- 人工发布并记录反馈。

现在不要提前做。

---

## P2｜PersonalOS V1

- [ ] 每天开工前更新当天 daily log。
- [ ] 每天只锁定 A/B/C 三个成果。
- [ ] 突然想到的新事情只写入 Inbox。
- [ ] 收工前做 10 分钟复盘。
- [ ] 7 天后做第一次 Weekly Review。

---

## Next Action 合格标准

错误：

- “继续开发工作台”
- “优化系统”
- “研究一下为什么不行”

正确：

- “复现百炼文本 `model_output_invalid`，保存真实响应并修到 live analysis 成功写入 Opportunity”
- “让指定真实账号的 2/2 商品都拥有 detail.json、本地图片和 SHA-256 manifest，并让验证 job 成功”
- “从一个真实 Opportunity 一直跑到 ZIP，记录第一个失败节点”

如果一个动作读完仍然不知道下一分钟该做什么，它还不够具体。