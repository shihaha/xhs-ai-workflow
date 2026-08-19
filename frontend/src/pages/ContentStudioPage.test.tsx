import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ContentStudioPage } from "./ContentStudioPage";

const revision = { id: "revision-1", number: 1, title: "露营清单", body: "正文", claims: [{ claim: "需求存在", evidence_ids: ["rank-item:7"] }], source_evidence_ids: ["rank-item:7"], image_plan: [{ page_number: 1, material_id: "image-1", role: "cover", headline: "露营清单", visual_direction: "清晰封面" }], model_provider: "bailian", model_name: "deepseek", prompt_version: "v1", usage: {}, attempts: [], created_at: "2026-08-17T08:00:00" };
const item = { id: "item-1", product_id: "product-1", opportunity_id: "opp-1", template_key: "list-v1", status: "review", evidence_ids: ["rank-item:7"], material_ids: [], image_material_ids: ["image-1"], cover_material_id: "image-1", research_facts: [{ fact: "需求存在", evidence_ids: ["rank-item:7"] }], current_revision: revision, revisions: [revision], reviews: [], export_availability: null, created_at: "2026-08-17T08:00:00", updated_at: "2026-08-17T08:00:00" };
const product = { id: "product-1", name: "露营产品", target_user: "露营用户", opportunity_id: "opp-1", materials: [{ id: "generated-1", product_id: "product-1", logical_name: "generated.png", version: 1, path: "content-generated/item-1/run-generation/generated-1.png", sha256: "0".repeat(64), size_bytes: 68, media_type: "image/png", kind: "output_image", availability: "available", created_at: "2026-08-17T08:00:00" }] };
const generationRun = { id: "run-generation", job_id: "job-generation", owner_product_id: "product-1", content_item_id: "item-1", revision_id: "revision-1", plan_entry_id: "image-1", capability: "generate", status: "succeeded", state_version: 2, provider: "bailian", model: "wan2.6-image", prompt_version: "image-v1", input_digest: "0".repeat(64), allowed_evidence_ids: ["rank-item:7"], allowed_material_ids: [], output_material_id: "generated-1", analysis_artifact_id: null, usage: { images: 1 }, duration_ms: 1200, attempts: [], error_category: null, error_detail: null, lease_token: null, lease_expires_at: null, created_at: "2026-08-17T08:00:00", updated_at: "2026-08-17T08:00:01", completed_at: "2026-08-17T08:00:01" } as const;
const analysisRun = { ...generationRun, id: "run-analysis", job_id: "job-analysis", plan_entry_id: null, capability: "analyze", model: "qwen-vl", output_material_id: null, analysis_artifact_id: 7, allowed_material_ids: ["generated-1"] } as const;

describe("ContentStudioPage", () => {
  it("shows a truthful empty content state", async () => {
    render(<ContentStudioPage loadStudio={vi.fn().mockResolvedValue({ products: [], items: [], packages: [] })} />);
    expect(await screen.findByText("No products or content items yet")).toBeVisible();
  });

  it("shows failed packages and does not call them ready", async () => {
    render(<ContentStudioPage loadStudio={vi.fn().mockResolvedValue({ products: [], items: [], packages: [{ id: "package-1", content_item_id: "item-1", revision_id: "revision-1", status: "failed", availability: "failed", path: "packages/item-1.zip", sha256: "0".repeat(64), size_bytes: 0, created_at: "2026-08-17T08:00:00" }] })} />);
    expect(await screen.findByText("Package failed")).toBeVisible();
    expect(screen.queryByText("Ready to publish")).not.toBeInTheDocument();
  });

  it("does not offer stale review actions after persisted export", async () => {
    render(<ContentStudioPage loadStudio={vi.fn().mockResolvedValue({ products: [], items: [{ ...item, status: "exported", export_availability: "available" }], packages: [] })} />);
    await screen.findByText("露营清单");
    expect(screen.queryByRole("button", { name: "Reject 露营清单" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Export 露营清单" })).not.toBeInTheDocument();
  });

  it("supports reject, regenerate, approve and export with exact revision IDs", async () => {
    const reviewItem = vi.fn().mockResolvedValue(item);
    const regenerateItem = vi.fn().mockResolvedValue(item);
    const exportItem = vi.fn().mockResolvedValue({ id: "package-2", status: "ready", availability: "available", path: "packages/item-1.zip" });
    const loadStudio = vi.fn()
      .mockResolvedValueOnce({ products: [], items: [item], packages: [] })
      .mockResolvedValueOnce({ products: [], items: [{ ...item, status: "rejected" }], packages: [] })
      .mockResolvedValueOnce({ products: [], items: [item], packages: [] })
      .mockResolvedValueOnce({ products: [], items: [{ ...item, status: "approved" }], packages: [] })
      .mockResolvedValueOnce({ products: [], items: [{ ...item, status: "exported", export_availability: "available" }], packages: [] });
    render(<ContentStudioPage loadStudio={loadStudio} reviewItem={reviewItem} regenerateItem={regenerateItem} exportItem={exportItem} />);

    await screen.findByText("露营清单");
    fireEvent.change(screen.getByLabelText("Review note for 露营清单"), { target: { value: "证据还不够清楚" } });
    fireEvent.click(screen.getByRole("button", { name: "Reject 露营清单" }));
    await waitFor(() => expect(reviewItem).toHaveBeenCalledWith("item-1", expect.objectContaining({ decision: "reject", expected_revision_id: "revision-1" })));

    fireEvent.click(await screen.findByRole("button", { name: "Regenerate 露营清单" }));
    await waitFor(() => expect(regenerateItem).toHaveBeenCalledWith("item-1", { expected_revision_id: "revision-1" }));

    fireEvent.change(await screen.findByLabelText("Review note for 露营清单"), { target: { value: "证据已经补齐" } });
    fireEvent.change(screen.getByLabelText("Visual check for image-1"), { target: { value: "封面文字清晰" } });
    fireEvent.click(screen.getByRole("button", { name: "Approve 露营清单" }));
    await waitFor(() => expect(reviewItem).toHaveBeenCalledWith("item-1", expect.objectContaining({ decision: "approve", visual_checks: [{ material_id: "image-1", passed: true, observation: "封面文字清晰" }] })));

    fireEvent.click(await screen.findByRole("button", { name: "Export 露营清单" }));
    await waitFor(() => expect(exportItem).toHaveBeenCalledWith("item-1", { expected_revision_id: "revision-1" }));
    expect(await screen.findByText("Package available: packages/item-1.zip")).toBeVisible();
    await waitFor(() => expect(loadStudio).toHaveBeenCalledTimes(5));
  });

  it("blocks duplicate content mutations while the first request is pending", async () => {
    let resolve!: (value: { path: string }) => void;
    const exportItem = vi.fn(() => new Promise<{ path: string }>(done => { resolve = done; }));
    render(<ContentStudioPage loadStudio={vi.fn().mockResolvedValue({ products: [], items: [{ ...item, status: "approved" }], packages: [] })} exportItem={exportItem} />);
    const button = await screen.findByRole("button", { name: "Export 露营清单" });
    fireEvent.click(button); fireEvent.click(button);
    expect(exportItem).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    resolve({ path: "content-packages/item.zip" });
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("submits one generation for a plan entry and renders the persisted managed output", async () => {
    let resolve!: (value: unknown) => void;
    const generateImage = vi.fn(() => new Promise(done => { resolve = done; }));
    const data = { products: [product], items: [item], packages: [], mediaRuns: { "item-1": [generationRun] }, assessments: {} };
    render(<ContentStudioPage loadStudio={vi.fn().mockResolvedValue(data)} generateImage={generateImage} />);

    const button = await screen.findByRole("button", { name: "Generate image for page 1" });
    fireEvent.click(button); fireEvent.click(button);
    expect(generateImage).toHaveBeenCalledTimes(1);
    expect(generateImage).toHaveBeenCalledWith("item-1", { expected_revision_id: "revision-1", image_plan_entry_id: "image-1" });
    expect(screen.getByText("wan2.6-image")).toBeVisible();
    expect(screen.getAllByText(/generated-1/).length).toBeGreaterThan(0);
    expect(screen.getByText(/available managed output_image/)).toBeVisible();
    resolve(generationRun);
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("shows visual advice without checking or approving the human review", async () => {
    const assessment = { run_id: "run-analysis", content_item_id: "item-1", revision_id: "revision-1", material_ids: ["generated-1"], provider: "bailian", model: "qwen-vl", prompt_version: "vision-v1", provider_request_id: "request-1", usage: { tokens: 2 }, duration_ms: 800, assessment: { summary: "画面与计划一致", plan_match: true, text_readability: "标题清晰", defects: [], safety_issues: [], suggestions: ["人工确认裁切"] } };
    render(<ContentStudioPage loadStudio={vi.fn().mockResolvedValue({ products: [product], items: [item], packages: [], mediaRuns: { "item-1": [analysisRun, generationRun] }, assessments: { "run-analysis": assessment } })} />);

    expect(await screen.findByText("AI visual advice — human review still required")).toBeVisible();
    expect(screen.getByText("画面与计划一致")).toBeVisible();
    expect(screen.getByLabelText("Visual check for image-1")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Approve 露营清单" })).toBeDisabled();
  });

  it("offers an explicit retry for a failed media run", async () => {
    const generateImage = vi.fn().mockResolvedValue(generationRun);
    const failed = { ...generationRun, id: "run-failed", status: "failed", output_material_id: null, error_category: "provider_unavailable", error_detail: "Media run failed." } as const;
    render(<ContentStudioPage loadStudio={vi.fn().mockResolvedValue({ products: [product], items: [item], packages: [], mediaRuns: { "item-1": [failed] }, assessments: {} })} generateImage={generateImage} />);

    fireEvent.click(await screen.findByRole("button", { name: "Retry image generation for page 1" }));
    await waitFor(() => expect(generateImage).toHaveBeenCalledWith("item-1", { expected_revision_id: "revision-1", image_plan_entry_id: "image-1" }));
    expect(screen.getByText(/provider_unavailable/)).toBeVisible();
  });
});
