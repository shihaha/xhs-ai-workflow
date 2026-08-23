import { expect, test, type Page } from "@playwright/test";

const now = "2026-08-23T15:50:00Z";
const job = {
  job_id: "job-preview-1",
  job_state: "needs_human",
  current_stage: "approval_required",
  error_category: "approval_required",
  retry_count: 0,
  current_run_id: "run-preview-1",
  authority_ambiguous: false,
  run_count: 1,
  pending_human_action_count: 1,
  evidence_count: 2,
  artifact_count: 0,
  created_at: now,
  updated_at: now,
};
const run = {
  run_id: "run-preview-1",
  job_id: "job-preview-1",
  goal: "根据已有榜单和账号证据，判断这个方向是否值得继续深入分析",
  state: "needs_human",
  model_name: "preview-model",
  prompt_version: "v1",
  step_count: 3,
  model_calls: 2,
  input_tokens: 420,
  output_tokens: 116,
  final_output: null,
  error_category: "approval_required",
  error_detail: "waiting",
  created_at: now,
  updated_at: now,
  completed_at: null,
};
const action = {
  id: "human-preview-1",
  run_id: "run-preview-1",
  job_id: "job-preview-1",
  tool_call_id: "analysis-preview-1",
  tool_name: "analysis.run_grounded",
  status: "pending",
  can_deny: true,
  can_approve: true,
  created_at: now,
  resolved_at: null,
};
const evidence = [
  { evidence_id: "rank-item:7", kind: "rank_item", account_user_id: "account-a", eligible_for_opportunity: false },
  { evidence_id: "account-note:12", kind: "account_note", account_user_id: "account-a", eligible_for_opportunity: false },
];
const capabilities = {
  cancel_job: true,
  deny_permission_action: true,
  approve_continuation: true,
  start_grounded_orchestration: true,
  continuation_reason: null,
};

async function routeAgentPreview(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    if (path === "/api/v1/agent-runtime/jobs") body = [job];
    else if (path === "/api/v1/agent-runtime/runs") body = [run];
    else if (path === "/api/v1/agent-runtime/human-actions") body = [action];
    else if (path === "/api/v1/agent-runtime/chatgpt-handoffs") body = [];
    else if (path === "/api/v1/agent-runtime/operator-capabilities") body = capabilities;
    else if (path === "/api/v1/analysis-evidence") body = evidence;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}

for (const scenario of [
  { name: "desktop", width: 1440, height: 1050, expectedColumns: 3 },
  { name: "mobile", width: 390, height: 844, expectedColumns: 1 },
]) {
  test(`Chinese-first Agent workbench stays usable on ${scenario.name}`, async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => consoleErrors.push(String(error)));
    await page.setViewportSize({ width: scenario.width, height: scenario.height });
    await routeAgentPreview(page);

    await page.goto("/agent");
    await expect(page.getByRole("heading", { name: "Agent 工作台" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "任务记录" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Agent 现在在做什么" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "等待你处理" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "可用证据" })).toBeVisible();
    await expect(page.getByRole("button", { name: "批准并继续" })).toBeVisible();

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(overflow).toBe(false);
    const columns = await page.locator(".agent-workspace-grid").evaluate((element) => {
      const value = getComputedStyle(element).gridTemplateColumns.trim();
      return value ? value.split(/\s+/).length : 0;
    });
    expect(columns).toBe(scenario.expectedColumns);
    expect(consoleErrors).toEqual([]);
  });
}
