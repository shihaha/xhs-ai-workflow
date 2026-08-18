import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OpportunitiesPage } from "./OpportunitiesPage";

describe("OpportunitiesPage", () => {
  it("shows a truthful empty state and model guidance", async () => {
    render(<OpportunitiesPage loadOpportunities={vi.fn().mockResolvedValue({ opportunities: [], products: [] })} />);
    expect(await screen.findByText("No evidence-backed opportunities yet")).toBeVisible();
  });

  it("creates a product bound to the selected persisted opportunity", async () => {
    const createProduct = vi.fn().mockResolvedValue({ id: "product-1", materials: [] });
    const loadOpportunities = vi.fn().mockResolvedValue({
      opportunities: [{ id: "opp-1", analysis_id: "analysis-1", title: "露营收纳", status: "升温", summary: "多条证据支持", evidence_ids: ["rank-item:7"], next_action: "验证产品", created_at: "2026-08-17T08:00:00" }],
      products: [],
    });
    render(<OpportunitiesPage loadOpportunities={loadOpportunities} createProduct={createProduct} />);

    await screen.findByText("露营收纳");
    fireEvent.change(screen.getByLabelText("Product name for 露营收纳"), { target: { value: "露营收纳清单" } });
    fireEvent.change(screen.getByLabelText("Target user for 露营收纳"), { target: { value: "首次自驾露营的人" } });
    fireEvent.click(screen.getByRole("button", { name: "Create product for 露营收纳" }));
    await waitFor(() => expect(createProduct).toHaveBeenCalledWith({ opportunity_id: "opp-1", name: "露营收纳清单", target_user: "首次自驾露营的人" }));
    expect(await screen.findByText("Created product product-1")).toBeVisible();
    await waitFor(() => expect(loadOpportunities).toHaveBeenCalledTimes(2));
  });

  it("keeps API failure visible and retryable", async () => {
    render(<OpportunitiesPage loadOpportunities={vi.fn().mockRejectedValue(new Error("offline"))} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load opportunities");
  });
});
