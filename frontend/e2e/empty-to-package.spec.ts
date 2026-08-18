import { expect, test } from "@playwright/test";

test("fresh temporary database reaches an available pending-publication package", async ({ page, request }, testInfo) => {
  const browserErrors: string[] = [];
  const suffix = `${testInfo.workerIndex}-${testInfo.repeatEachIndex}-${Date.now()}`;
  const accountId = `e2e-author-${suffix}`;
  const accountName = `受控真实链路账号-${suffix}`;
  const opportunityTitle = `受控露营收纳机会-${accountId}`;
  const productName = `受控露营收纳产品-${suffix}`;
  const contentTitle = `${productName}清单`;
  page.on("console", message => { if (message.type() === "error") browserErrors.push(message.text()); });
  page.on("pageerror", error => browserErrors.push(error.message));
  const snapshotPayload = {
    source_date: "2026-08-17", collected_at: "2026-08-17T08:00:00Z", board: "热卖榜", dimension: "优秀账号",
    source_url: "https://qianfan.example/rank", raw_evidence: { fixture: "controlled-task9" },
    items: [{ rank_no: 1, author_name: accountName, user_id: accountId, source_url: `https://www.xiaohongshu.com/explore/rank-note-${suffix}`, gmv_range: "1万-5万", pay_rate_range: "70%-90%", read_range: "10万以上", raw_evidence: { fixture: "controlled-task9-row" } }],
  };

  await page.goto("/radar");
  await page.getByLabel("Captured ranking snapshot JSON").fill(JSON.stringify(snapshotPayload));
  await page.getByRole("button", { name: "Import captured snapshot" }).click();
  await expect(page.getByText(/Snapshot \d+ persisted/)).toBeVisible();
  await expect(page.getByRole("link", { name: accountName })).toBeVisible();
  await page.getByRole("link", { name: accountName }).click();
  await expect(page.getByText("Device available: controlled_device_ready")).toBeVisible();
  await page.getByLabel("Expected shop products").fill("1");
  await page.getByLabel("Verification evidence directory").fill("fixtures/shop-account");
  await page.getByRole("button", { name: "Queue device collection" }).click();
  await expect(page.getByText(/Queued job/)).toBeVisible();

  await expect.poll(async () => {
    const response = await request.get("http://127.0.0.1:8000/api/v1/jobs");
    const jobs = await response.json() as Array<{ state: string; type: string }>;
    return jobs.find((job: { state: string; type: string; input?: { account_user_id?: string } }) => job.type === "android_shop_collection" && job.input?.account_user_id === accountId)?.state;
  }).toBe("succeeded");
  await page.reload();
  await expect(page.getByText("1 / 1 verified or collected")).toBeVisible();
  const deepEvidence = page.getByLabel(/^artifact:/);
  await deepEvidence.check();
  await page.getByRole("button", { name: "Generate opportunity analysis" }).click();
  await expect(page.getByText(/Opportunity analysis request completed/)).toBeVisible();

  await page.goto("/opportunities");
  await expect(page.getByRole("heading", { name: opportunityTitle })).toBeVisible();
  await page.getByLabel(`Product name for ${opportunityTitle}`).fill(productName);
  await page.getByLabel(`Target user for ${opportunityTitle}`).fill("需要整理露营装备的用户");
  await page.getByRole("button", { name: `Create product for ${opportunityTitle}` }).click();
  await expect(page.getByText(/Created product/)).toBeVisible();

  await page.goto("/content");
  const productRecord = page.getByRole("heading", { name: productName }).locator("..").locator("..");
  await productRecord.getByText("Add managed material").click();
  const materialForm = productRecord.getByText("Add managed material").locator("..");
  await materialForm.getByLabel("Logical filename").fill("source.txt");
  await materialForm.getByLabel("Runtime-relative source path").fill("fixtures/source.txt");
  await materialForm.getByLabel("Media type").fill("text/plain");
  await materialForm.getByRole("button", { name: "Add material" }).click();
  await expect(page.getByText(/Material accepted/)).toBeVisible();
  await page.reload();
  const reloadedProductRecord = page.getByRole("heading", { name: productName }).locator("..").locator("..");
  await reloadedProductRecord.getByText("Add managed material").click();
  const imageForm = reloadedProductRecord.getByText("Add managed material").locator("..");
  await imageForm.getByLabel("Logical filename").fill("cover.png");
  await imageForm.getByLabel("Runtime-relative source path").fill("fixtures/cover.png");
  await imageForm.getByLabel("Media type").fill("image/png");
  await imageForm.getByLabel("Material kind").selectOption("output_image");
  await imageForm.getByRole("button", { name: "Add material" }).click();
  await expect(page.getByText(/Material accepted/)).toBeVisible();
  await page.reload();

  const materials = await request.get("http://127.0.0.1:8000/api/v1/products").then(response => response.json()) as Array<{ materials: Array<{ id: string; kind: string }> }>;
  const createdProduct = (materials as Array<{ id: string; name?: string; materials: Array<{ id: string; kind: string }> }>).find(product => product.name === productName)!;
  const sourceId = createdProduct.materials.find(material => material.kind === "source")!.id;
  const imageId = createdProduct.materials.find(material => material.kind === "output_image")!.id;
  const evidence = await request.get(`http://127.0.0.1:8000/api/v1/analysis-evidence?account_user_id=${encodeURIComponent(accountId)}`).then(response => response.json()) as Array<{ evidence_id: string; eligible_for_opportunity: boolean }>;
  const evidenceId = evidence.find(item => item.eligible_for_opportunity)!.evidence_id;

  const finalProductRecord = page.getByRole("heading", { name: productName }).locator("..").locator("..");
  await finalProductRecord.getByText("Create model draft").click();
  const draftForm = finalProductRecord.getByText("Create model draft").locator("..");
  await draftForm.getByLabel("Evidence IDs, comma-separated").fill(evidenceId);
  await draftForm.getByLabel("Source material IDs, comma-separated").fill(sourceId);
  await draftForm.getByLabel("Ordered image material IDs").fill(imageId);
  await draftForm.getByLabel("Research fact").fill("受控证据证明需求存在");
  await draftForm.getByRole("button", { name: "Generate content draft" }).click();
  await expect(page.getByText(/Content draft request completed/)).toBeVisible();
  await page.reload();

  await page.getByLabel(`Review note for ${contentTitle}`).fill("人工核验通过");
  await page.getByLabel(new RegExp(`Visual check for ${imageId}`)).fill("封面清晰且与正文一致");
  await page.getByRole("button", { name: `Approve ${contentTitle}` }).click();
  await expect(page.getByText(/Approval persisted/)).toBeVisible();
  await page.getByRole("button", { name: `Export ${contentTitle}` }).click();
  await expect(page.getByText(/Package available: content-packages\//)).toBeVisible();

  const items = await request.get("http://127.0.0.1:8000/api/v1/content-items").then(response => response.json()) as Array<{ id: string; product_id: string }>;
  const currentItem = items.find(item => item.product_id === createdProduct.id)!;
  const packages = await request.get("http://127.0.0.1:8000/api/v1/content-packages").then(response => response.json()) as Array<{ content_item_id: string; status: string; availability: string }>;
  expect(packages.find(pkg => pkg.content_item_id === currentItem.id)).toEqual(expect.objectContaining({ status: "ready", availability: "available" }));
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("available-package.png") });
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto("/content");
  await expect(page.getByRole("heading", { name: "Content studio" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Workbench" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("mobile-content.png") });
  expect(browserErrors).toEqual([]);
});
