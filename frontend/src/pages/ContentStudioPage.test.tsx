import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ContentStudioPage } from "./ContentStudioPage";

const revision = { id: "revision-1", number: 1, title: "露营清单", body: "正文", claims: [{ claim: "需求存在", evidence_ids: ["rank-item:7"] }], source_evidence_ids: ["rank-item:7"], image_plan: [{ page_number: 1, material_id: "image-1", role: "cover", headline: "露营清单", visual_direction: "清晰封面" }], model_provider: "bailian", model_name: "deepseek", prompt_version: "v1", usage: {}, attempts: [], created_at: "2026-08-17T08:00:00" };
const item = { id: "item-1", product_id: "product-1", opportunity_id: "opp-1", template_key: "list-v1", status: "review", evidence_ids: ["rank-item:7"], material_ids: [], image_material_ids: ["image-1"], cover_material_id: "image-1", research_facts: [{ fact: "需求存在", evidence_ids: ["rank-item:7"] }], current_revision: revision, revisions: [revision], reviews: [], export_availability: null, created_at: "2026-08-17T08:00:00", updated_at: "2026-08-17T08:00:00" };

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
});
