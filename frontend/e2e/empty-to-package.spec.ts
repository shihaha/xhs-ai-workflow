import { expect, test } from "@playwright/test";

test("fresh temporary database reaches an available pending-publication package", async ({ page, request }, testInfo) => {
  const browserErrors: string[] = [];
  const suffix = `${testInfo.workerIndex}-${testInfo.repeatEachIndex}-${Date.now()}`;
  const productName = `受控露营收纳产品-${suffix}`;
  const contentTitle = `${productName}清单`;
  page.on("console", message => { if (message.type() === "error") browserErrors.push(message.text()); });
  page.on("pageerror", error => browserErrors.push(error.message));
  await page.goto("/radar");
  await page.getByLabel("Expected rows per ranking scope").fill("1");
  await page.getByRole("button", { name: "Start automatic Qianfan collection" }).click();
  await expect(page.getByText(/Collection [0-9a-f-]+ reserved as one batch of 8 scope jobs/)).toBeVisible();
  await expect(page.getByText("Complete: 8/8 scopes succeeded.")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/Selector profile: controlled-qianfan-v1/)).toBeVisible();
  const collectionHeading = page.getByRole("heading", { name: /^Collection [0-9a-f-]+$/ });
  const collectionId = (await collectionHeading.textContent())!.replace("Collection ", "").trim();
  const accountIds = [`controlled-account-a-${collectionId}`, `controlled-account-b-${collectionId}`];
  const accountNames = [`受控千帆账号-a-${collectionId}`, `受控千帆账号-b-${collectionId}`];
  await page.reload();
  for (const accountName of accountNames) await expect(page.getByRole("link", { name: accountName })).toBeVisible();
  const collectedEvidence: Record<string, { note: string; shop: string }> = {};
  for (let index = 0; index < accountIds.length; index += 1) {
    await page.goto("/radar");
    await page.getByRole("link", { name: accountNames[index] }).click();
    await expect(page.getByText("Device available: controlled_device_ready")).toBeVisible();
    await expect(page.getByText(/latest 10 unique public notes/i)).toBeVisible();
    await page.getByRole("button", { name: "Collect account and notes" }).click();
    await expect(page.getByText(/Account collection [0-9a-f-]+ queued/)).toBeVisible();
    await expect(page.getByRole("link", { name: "Open note source" })).toBeVisible({ timeout: 15_000 });
    const collectedNoteEvidence = page.getByLabel(/^account-note:/).first();
    await expect(collectedNoteEvidence).toBeVisible();
    const noteId = await collectedNoteEvidence.getAttribute("aria-label");
    expect(noteId).toBeTruthy();
    await page.getByLabel("Expected shop products").fill("1");
    await page.getByLabel("Verification evidence directory").fill("fixtures/shop-account");
    await page.getByRole("button", { name: "Queue device collection" }).click();
    const queuedNotice = page.getByText(/Queued job/);
    await expect(queuedNotice).toBeVisible();
    const shopJobId = (await queuedNotice.textContent())!.replace("Queued job ", "").trim();
    await expect.poll(async () => {
      const response = await request.get("http://127.0.0.1:8000/api/v1/jobs");
      const jobs = await response.json() as Array<{ id?: string; state: string }>;
      return jobs.find(job => job.id === shopJobId)?.state;
    }).toBe("succeeded");
    await page.reload();
    await expect(page.getByText("1 / 1 verified or collected").last()).toBeVisible();
    const deepEvidence = page.getByLabel(/^artifact:/).last();
    const shopId = await deepEvidence.getAttribute("aria-label");
    expect(shopId).toBeTruthy();
    collectedEvidence[accountIds[index]] = { note: noteId!, shop: shopId! };
  }
  const accountJobs = await request.get("http://127.0.0.1:8000/api/v1/jobs").then(response => response.json());
  expect(JSON.stringify(accountJobs)).not.toContain("controlled-xsec-must-not-persist");

  await page.goto("/opportunities");
  for (const accountName of accountNames) await page.getByLabel(`Select ${accountName}`).check();
  await page.getByRole("button", { name: "Run cross-account clustering" }).click();
  await expect(page.getByText(/Cross-account analysis completed/)).toBeVisible();
  const analyses = await request.get("http://127.0.0.1:8000/api/v1/analyses").then(response => response.json()) as Array<{ status: string; error_category: string | null; error_detail: string | null }>;
  expect(analyses.at(-1)).toEqual(expect.objectContaining({ status: "succeeded", error_category: null }));
  const opportunityTitle = `受控露营收纳机会-${accountIds.join("-")}`;
  await expect(page.getByRole("heading", { name: opportunityTitle })).toBeVisible();
  await expect(page.getByText("Evidence level:").locator("..")).toContainText("warming_candidate");
  await page.getByRole("button", { name: `Approve ${opportunityTitle}` }).click();
  await expect(page.getByText(`Approved ${opportunityTitle}`)).toBeVisible();
  const opportunities = await request.get("http://127.0.0.1:8000/api/v1/opportunities").then(response => response.json()) as Array<{ id: string; title: string; review_status: string; supporting_account_count: number }>;
  const approvedOpportunity = opportunities.find(item => item.title === opportunityTitle)!;
  expect(approvedOpportunity).toEqual(expect.objectContaining({ review_status: "approved", supporting_account_count: 2 }));
  const createProduct = await request.post("http://127.0.0.1:8000/api/v1/products", { data: { name: productName, target_user: "需要整理露营装备的用户", opportunity_id: approvedOpportunity.id } });
  expect(createProduct.status(), await createProduct.text()).toBe(201);
  const shopEvidenceId = collectedEvidence[accountIds[0]].shop;

  await page.goto("/content");
  const productRecord = page.getByRole("heading", { name: productName }).locator("..").locator("..");
  await productRecord.getByText("Add existing material (manual)").click();
  const materialForm = productRecord.getByText("Add existing material (manual)").locator("..");
  await materialForm.getByLabel("Logical filename").fill("source.txt");
  await materialForm.getByLabel("Runtime-relative source path").fill("fixtures/source.txt");
  await materialForm.getByLabel("Media type").fill("text/plain");
  await materialForm.getByRole("button", { name: "Add manual material" }).click();
  await expect(page.getByText(/Manual material accepted/)).toBeVisible();
  await page.reload();
  const reloadedProductRecord = page.getByRole("heading", { name: productName }).locator("..").locator("..");
  await reloadedProductRecord.getByText("Add existing material (manual)").click();
  const imageForm = reloadedProductRecord.getByText("Add existing material (manual)").locator("..");
  await imageForm.getByLabel("Logical filename").fill("collected-product.png");
  await imageForm.getByLabel("Runtime-relative source path").fill("fixtures/shop-account/product-1/images/product.png");
  await imageForm.getByLabel("Media type").fill("image/png");
  await imageForm.getByLabel("Material kind").selectOption("output_image");
  await imageForm.getByRole("button", { name: "Add manual material" }).click();
  await expect(page.getByText(/Manual material accepted/)).toBeVisible();
  await page.reload();

  const materials = await request.get("http://127.0.0.1:8000/api/v1/products").then(response => response.json()) as Array<{ materials: Array<{ id: string; kind: string }> }>;
  const createdProduct = (materials as Array<{ id: string; name?: string; materials: Array<{ id: string; kind: string }> }>).find(product => product.name === productName)!;
  const sourceId = createdProduct.materials.find(material => material.kind === "source")!.id;
  const imageId = createdProduct.materials.find(material => material.kind === "output_image")!.id;
  const finalProductRecord = page.getByRole("heading", { name: productName }).locator("..").locator("..");
  await finalProductRecord.getByText("Create model draft").click();
  const draftForm = finalProductRecord.getByText("Create model draft").locator("..");
  await draftForm.getByLabel("Evidence IDs, comma-separated").fill(shopEvidenceId!);
  await draftForm.getByLabel("Source material IDs, comma-separated").fill(sourceId);
  await draftForm.getByLabel("Ordered image material IDs").fill(imageId);
  await draftForm.getByLabel("Research fact").fill("受控证据证明需求存在");
  await draftForm.getByRole("button", { name: "Generate content draft" }).click();
  await expect(page.getByText(/Content draft request completed/)).toBeVisible();
  await page.reload();

  const planningReview = page.getByRole("heading", { name: contentTitle }).locator("..").locator("..").locator("..");
  await planningReview.getByRole("button", { name: "Generate image for page 1" }).click();
  await expect(page.getByText(/Image generation queued/)).toBeVisible();
  await expect(planningReview.getByText("succeeded").last()).toBeVisible({ timeout: 15_000 });
  const mediaRuns = await request.get(`http://127.0.0.1:8000/api/v1/content-items/${(await request.get("http://127.0.0.1:8000/api/v1/content-items").then(response => response.json()) as Array<{ id: string; product_id: string }>).find(value => value.product_id === createdProduct.id)!.id}/media-runs`).then(response => response.json()) as Array<{ capability: string; status: string; output_material_id: string | null }>;
  const generatedImageId = mediaRuns.find(value => value.capability === "generate" && value.status === "succeeded")!.output_material_id!;
  await page.getByLabel(generatedImageId).check();
  await planningReview.getByRole("button", { name: "Analyze selected images" }).click();
  await expect(page.getByText(/Visual analysis queued/)).toBeVisible();
  await expect(planningReview.getByText("AI visual advice — human review still required")).toBeVisible({ timeout: 15_000 });
  await expect(planningReview.getByText("受控视觉检查建议人工确认")).toBeVisible();
  await page.getByLabel(`Review note for ${contentTitle}`).fill("替换为已生成图片后再审核");
  await page.getByRole("button", { name: `Reject ${contentTitle}` }).click();
  await expect(page.getByText(/Rejection persisted/)).toBeVisible();

  const productAfterGeneration = page.getByRole("heading", { name: productName }).locator("..").locator("..");
  await productAfterGeneration.getByText("Create model draft").click();
  const generatedDraftForm = productAfterGeneration.getByText("Create model draft").locator("..");
  await generatedDraftForm.getByLabel("Evidence IDs, comma-separated").fill(shopEvidenceId!);
  await generatedDraftForm.getByLabel("Source material IDs, comma-separated").fill(sourceId);
  await generatedDraftForm.getByLabel("Ordered image material IDs").fill(generatedImageId);
  await generatedDraftForm.getByLabel("Research fact").fill("受控证据证明需求存在");
  await generatedDraftForm.getByRole("button", { name: "Generate content draft" }).click();
  await expect(page.getByText(/Content draft request completed/)).toBeVisible();
  await page.reload();

  await page.getByLabel(`Review note for ${contentTitle}`).fill("人工核验通过");
  await page.getByLabel(new RegExp(`Visual check for ${generatedImageId}`)).fill("封面清晰且与正文一致");
  await page.getByRole("button", { name: `Approve ${contentTitle}` }).click();
  await expect(page.getByText(/Approval persisted/)).toBeVisible();
  await page.getByRole("button", { name: `Export ${contentTitle}` }).click();
  await expect(page.getByText(/Package available: content-packages\//)).toBeVisible();

  const items = await request.get("http://127.0.0.1:8000/api/v1/content-items").then(response => response.json()) as Array<{ id: string; product_id: string; status: string; image_material_ids: string[] }>;
  const currentItem = items.find(item => item.product_id === createdProduct.id && item.status === "exported")!;
  expect(currentItem.image_material_ids).toEqual([generatedImageId]);
  const packages = await request.get("http://127.0.0.1:8000/api/v1/content-packages").then(response => response.json()) as Array<{ content_item_id: string; status: string; availability: string }>;
  expect(packages.find(pkg => pkg.content_item_id === currentItem.id)).toEqual(expect.objectContaining({ status: "ready", availability: "available" }));
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("available-package.png") });
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/content");
    await expect(page.getByRole("heading", { name: "Content studio" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Workbench" })).toBeVisible();
    const overflow = await page.evaluate(() => Array.from(document.querySelectorAll("body *"))
      .filter(element => element.getBoundingClientRect().right > window.innerWidth + 1)
      .slice(0, 5)
      .map(element => `${element.tagName.toLowerCase()}.${element.className}`));
    expect(overflow).toEqual([]);
  }
  await page.setViewportSize({ width: 320, height: 900 });
  await page.screenshot({ path: testInfo.outputPath("mobile-content.png") });
  expect(browserErrors).toEqual([]);
});
