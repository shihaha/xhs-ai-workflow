import { expect, test, type Page } from "@playwright/test";

const controlledRadar = {
  summary: {
    generated_on: "2026-08-24",
    total_direction_count: 3,
    new_today_count: 2,
    warming_count: 1,
    validated_count: 2,
    pending_decision_count: 2,
    approved_count: 1,
    linked_product_count: 1,
  },
  directions: [
    {
      opportunity_id: "controlled-opportunity-1",
      title: "人格测试 / 七宗罪方向",
      summary: "3 个独立账号反复销售相近的人格测试和结果报告。",
      evidence_level: "validated_candidate",
      review_status: "pending_review",
      can_follow_up: true,
      journey_stage: "demand_decision",
      is_new_today: true,
      supporting_account_count: 3,
      supporting_product_count: 9,
      supporting_note_count: 5,
      image_evidence_count: 27,
      representative_products: [
        {
          account_user_id: "controlled-account-a",
          product_id: "controlled-product-a",
          title: "七宗罪人格测试完整版",
          source_url: "https://www.xiaohongshu.com/",
          price: "¥19.9",
          sold: "1.2万+",
          image_evidence_count: 6,
          image_artifact_ids: [21],
        },
        {
          account_user_id: "controlled-account-b",
          product_id: "controlled-product-b",
          title: "黑暗人格测试结果报告",
          source_url: "https://www.xiaohongshu.com/",
          price: "¥29.9",
          sold: "8300+",
          image_evidence_count: 5,
          image_artifact_ids: [],
        },
      ],
      linked_products: [],
      next_business_action: "查看支撑账号、商品和图片证据后，决定跟进、继续观察或拒绝。",
      created_at: "2026-08-24T09:30:00",
      reviewed_at: null,
    },
    {
      opportunity_id: "controlled-opportunity-2",
      title: "运营资料模板",
      summary: "当前有重复销售迹象，但共同具体需求证据还不足。",
      evidence_level: "warming_candidate",
      review_status: "pending_review",
      can_follow_up: false,
      journey_stage: "demand_decision",
      is_new_today: true,
      supporting_account_count: 2,
      supporting_product_count: 4,
      supporting_note_count: 2,
      image_evidence_count: 11,
      representative_products: [],
      linked_products: [],
      next_business_action: "继续观察并补齐共同具体需求证据。",
      created_at: "2026-08-24T08:00:00",
      reviewed_at: null,
    },
    {
      opportunity_id: "controlled-opportunity-3",
      title: "历史产品方向",
      summary: "已有旧产品工作区，但没有新的 Product Definition Gate 证明。",
      evidence_level: "validated_candidate",
      review_status: "approved",
      can_follow_up: true,
      journey_stage: "legacy_product_workspace",
      is_new_today: false,
      supporting_account_count: 4,
      supporting_product_count: 12,
      supporting_note_count: 0,
      image_evidence_count: 31,
      representative_products: [],
      linked_products: [
        {
          product_id: "controlled-legacy-product",
          name: "历史效率工具",
          target_user: "需要提升效率的用户",
          created_at: "2026-08-20T10:00:00",
        },
      ],
      next_business_action: "已有历史产品工作区；进入 Stage 7 产品定义前先核对现有产品资料。",
      created_at: "2026-08-20T10:00:00",
      reviewed_at: "2026-08-20T11:00:00",
    },
  ],
};

async function openControlledRadar(page: Page) {
  await page.route("**/api/v1/business/demand-radar/controlled-opportunity-1/media/21", route => route.fulfill({
    status: 200,
    contentType: "image/png",
    body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"),
  }));
  await page.route("**/api/v1/business/demand-radar", route => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(controlledRadar),
  }));
  await page.goto("/opportunities");
  await expect(page.getByRole("heading", { name: "需求雷达" })).toBeVisible();
}

test("desktop demand radar prioritizes business decisions without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openControlledRadar(page);

  await expect(page.getByText("今日新增").first()).toBeVisible();
  await expect(page.getByText("待你决定").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "人格测试 / 七宗罪方向" })).toBeVisible();
  await expect(page.getByText("价格 ¥19.9")).toBeVisible();
  await expect(page.getByText("销量 1.2万+")).toBeVisible();
  await expect(page.getByAltText("七宗罪人格测试完整版 证据图 1")).toBeVisible();
  await page.getByRole("button", { name: "放大查看七宗罪人格测试完整版 证据图 1" }).click();
  await expect(page.getByRole("dialog", { name: "商品证据大图" })).toBeVisible();
  await page.getByRole("button", { name: "关闭大图" }).click();
  await expect(page.getByRole("button", { name: "跟进这个方向" })).toHaveCount(1);
  await expect(page.getByText("共同具体需求尚未被持久化证明，当前不能跟进。")).toBeVisible();
  await expect(page.getByText(/不等于新的 Product Definition Gate 已通过/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440);
});

test("mobile demand radar keeps the decision journey in one column without overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openControlledRadar(page);

  await expect(page.getByRole("heading", { name: "人格测试 / 七宗罪方向" })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "今天需要关注" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
