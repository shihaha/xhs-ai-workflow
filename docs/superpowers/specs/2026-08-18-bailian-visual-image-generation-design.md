# 百炼视觉理解与图片生成补漏设计

## 1. 背景与目标

当前系统已实现百炼文本结构化生成、图片素材校验、人工逐图审核和待发布 ZIP，但图片必须由操作者预先放入 runtime。受控 E2E 预写测试 PNG，只证明“已有图片可以打包”，没有证明系统具备视觉理解和图片生成。

本设计补齐：

`内容图片规划 → 通义/万相生成图片 → 受管素材落库 → 通义视觉检查 → 人工逐图确认 → ZIP`

自动发布仍不在范围内。

## 2. 能力边界

将当前单一 `ModelAdapter` 拆分为三个明确、可替换能力：

- `StructuredTextAdapter.generate_structured`：保留现有 DeepSeek 文本路径。
- `VisionAdapter.analyze_images`：图片理解与结构化视觉检查。
- `ImageGenerationAdapter.generate_images`：根据受控提示生成图片字节与元数据。

百炼实现使用同一受信 API Key，但文本、视觉和图片生成模型分别配置：

- `XHS_BAILIAN_TEXT_MODEL`
- `XHS_BAILIAN_VISION_MODEL`
- `XHS_BAILIAN_IMAGE_MODEL`

具体模型名和 endpoint 由 Settings 提供固定值，HTTP 请求不能覆盖 base URL、模型名或输出目录。

## 3. 任务与持久事实

新增保留任务：

- `content_image_generation`
- `content_image_analysis`

新增 `content_media_runs` 持久表：

- run ID、job ID、content item ID、revision ID、image-plan entry ID。
- capability：`generate` 或 `analyze`。
- status：与 job 对齐的真实状态。
- provider、model、prompt version。
- allowed evidence/material IDs。
- output material ID 或分析 artifact ID。
- usage、duration、净化后的 attempts/error category。
- created/updated/completed 时间。

同一 content item/revision/plan entry 同时只允许一个未完成生成任务。重试创建新 run，旧 run 保留审计。

## 4. 图片生成流程

1. 服务端加载当前 content item、current revision 和 image plan。
2. 验证 product/opportunity/evidence/material 信任链仍有效。
3. 只允许选择当前 image plan 的 entry；提示词由服务端模板和已批准事实构造。
4. 创建 durable job/run，再调用图片生成适配器。
5. 校验返回字节的大小、MIME、完整解码、像素上限和真实格式。
6. 在受管 runtime 路径创建 cleanup reservation，再写文件。
7. 在同一数据库事务中创建 `output_image` MaterialRecord、绑定生成 provenance，并完成 run/job。
8. commit 确认不明时使用新会话精确核对，不能出现文件存在但无 material，或 material 成功但文件被删。

生成失败、限流、超时、认证失败和安全拒绝必须形成持久 failed/needs_human 事实，不创建成功素材。

## 5. 视觉理解流程

视觉分析输入只能是当前 content item 绑定的受管图片素材：

- 服务端有界读取并验证 hash、大小、MIME 和文件身份。
- 不接受任意网络 URL、任意本地绝对路径或 runtime 外文件。
- 模型返回严格结构：画面摘要、文字可读性、内容与 image-plan 匹配度、明显瑕疵、安全问题和建议。
- 输出必须通过 Pydantic 校验，并保存 provider request ID、模型、提示词版本、用量和耗时。

视觉结果只是机器建议。现有 `ReviewCreate.visual_checks` 仍要求人工对每张图片明确 pass；模型不能自动把内容项改为 approved。

## 6. API

- `POST /api/v1/content-items/{item_id}/image-generations`
  - body：`expected_revision_id`、`image_plan_entry_id`。
  - 返回 HTTP 202 和 run/job。
- `POST /api/v1/content-items/{item_id}/image-analyses`
  - body：`expected_revision_id`、严格 material IDs。
  - 返回 HTTP 202 和 run/job。
- `GET /api/v1/content-items/{item_id}/media-runs`
- `GET /api/v1/content-media-runs/{run_id}`

通用 Jobs API 不得 claim、上传专用 artifact 或伪造完成；只允许受控取消。

## 7. 文件与清理安全

- 生成文件只进入 `content-generated/{content_item_id}/{run_id}/...`。
- 名称由服务端生成 canonical UUID，不接受用户文件名。
- 生成前建立 durable cleanup outbox；成功 material 与 cleanup cancel 同事务。
- 失败或 CAS loss 保留文件并进入现有隔离/延迟 GC，不在请求路径直接删除。
- 视觉分析 artifact 走 JobArtifact 证据边界，不暴露原始密钥或完整 provider body。
- 导出继续验证图片 hash、真实解码、顺序、封面、视觉检查和人工审核。

## 8. 前端

Content Studio 增加：

- 按 image-plan entry 生成图片。
- 真实 queued/running/needs_human/failed/succeeded 状态。
- 显示生成模型、耗时、用量、错误分类和产物素材。
- 发起视觉检查并展示结构化建议。
- 人工逐图通过/退回；未通过不能导出。

保留“添加已有素材”，但明确标注为人工素材，不再把它视为模型生成。

## 9. 测试与验收

### 自动测试

- 三种 provider-neutral adapter 协议和配置独立性。
- 认证、429、5xx、超时、响应 schema、图片字节/MIME/像素与大小边界。
- run/job/material/file/cleanup 的原子性和 commit-ack 不确定性。
- 生成素材必须绑定当前 revision 和 image-plan entry。
- 视觉分析拒绝 runtime 外路径、被篡改素材和跨产品素材。
- 模型视觉通过不能绕过人工 visual checks。
- 前端状态、防双击、失败重试和人工审核。
- 受控 E2E 使用测试图片生成适配器产生真实可解码 PNG，再经过测试视觉适配器、人工检查和 ZIP；禁止预写 placeholder 图片代替生成步骤。

### Live 验收

用户将百炼 Key 配置到本机环境变量，并确认账号有视觉与图片生成模型权限/额度。至少完成一次真实图片生成和一次真实视觉分析，核对百炼 request ID、用量、受管文件和数据库一致性。未完成前保持 `not_run`。

## 10. 非目标

- 不自动发布小红书。
- 不让模型自动批准内容。
- 不接入本地 ComfyUI、其他付费平台或第二套图片文件系统。
- 不允许 API 传任意模型、endpoint、绝对路径或远程图片 URL。
- 不在没有百炼 Key 时使用占位图片冒充 live 成功。

