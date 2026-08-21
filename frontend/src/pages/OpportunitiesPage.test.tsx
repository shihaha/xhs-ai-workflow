import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OpportunitiesPage } from "./OpportunitiesPage";

const account = (id: string) => ({ user_id: id, account_name: `Account ${id}`, score: null, score_status: "insufficient_metrics" as const, ranking_evidence_count: 1, best_rank: 1, evidence: 0, credibility: 0, accessibility: 0, fans: 0, gmv: "—", pay: "—", read: "—", nday: 1, nboard: 1 });
const evidence = (id: string) => [
  { evidence_id: `artifact:${id === "a" ? 1 : 2}`, kind: "shop_collection_result", account_user_id: id, eligible_for_opportunity: true },
  { evidence_id: `account-note:${id === "a" ? 10 : 20}`, kind: "account_note", account_user_id: id, eligible_for_opportunity: true },
];
const opportunity = {
  id: "opp-1", analysis_id: "analysis-1", title: "Shared demand", status: "升温", summary: "Two accounts support this", evidence_ids: ["artifact:1", "account-note:10", "artifact:2", "account-note:20"],
  review_status: "pending_review" as const, evidence_level: "warming_candidate" as const, supporting_account_count: 2,
  supporting_accounts: [
    { account_user_id: "a", shop_evidence_ids: ["artifact:1"], note_evidence_ids: ["account-note:10"] },
    { account_user_id: "b", shop_evidence_ids: ["artifact:2"], note_evidence_ids: ["account-note:20"] },
  ],
  supporting_products: [{ account_user_id: "a", evidence_id: "artifact:1", product_id: "p1", title: "Product A", source_url: "https://example.com/p1", image_evidence_count: 2 }],
  supporting_notes: [{ account_user_id: "b", evidence_id: "account-note:20", note_id: "n2", title: "Note B", source_url: "https://example.com/n2" }],
  reviewed_at: null, rejection_reason: null, next_action: "human review", created_at: "2026-08-20T08:00:00",
};

describe("OpportunitiesPage", () => {
  it("shows the Phase A boundary and a truthful empty state", async () => {
    render(<OpportunitiesPage loadOpportunities={vi.fn().mockResolvedValue({ opportunities: [], accounts: [], evidence: [] })} />);
    expect(await screen.findByText("暂时没有跨账号候选")).toBeVisible();
    expect(screen.getByText(/Phase B 尚未开始/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /创建产品/ })).not.toBeInTheDocument();
  });

  it("submits two complete accounts with their trusted shop and note evidence", async () => {
    const createAnalysis = vi.fn().mockResolvedValue({ status: "succeeded" });
    const load = vi.fn().mockResolvedValue({ opportunities: [], accounts: [account("a"), account("b")], evidence: [...evidence("a"), ...evidence("b")] });
    render(<OpportunitiesPage loadOpportunities={load} createAnalysis={createAnalysis} />);
    await screen.findByText("Account a · 店铺证据 1 · 笔记证据 1 · 完整");
    fireEvent.click(screen.getByLabelText("选择 Account a"));
    fireEvent.click(screen.getByLabelText("选择 Account b"));
    fireEvent.click(screen.getByRole("button", { name: "运行跨账号需求分析" }));
    await waitFor(() => expect(createAnalysis).toHaveBeenCalledWith({
      analysis_type: "account_opportunity",
      account_user_ids: ["a", "b"],
      evidence_ids: ["artifact:1", "account-note:10", "artifact:2", "account-note:20"],
    }));
    expect(await screen.findByText(/跨账号分析已完成/)).toBeVisible();
  });

  it("renders evidence ownership and performs one-way human review", async () => {
    const review = vi.fn().mockResolvedValue({ ...opportunity, review_status: "approved" });
    const load = vi.fn().mockResolvedValue({ opportunities: [opportunity], accounts: [account("a"), account("b")], evidence: [...evidence("a"), ...evidence("b")] });
    render(<OpportunitiesPage loadOpportunities={load} reviewOpportunity={review} />);
    expect(await screen.findByText("升温候选 · 2 个支撑账号")).toBeVisible();
    expect(screen.getByText(/artifact:1/)).toBeVisible();
    expect(screen.getByText("Product A")).toBeVisible();
    expect(screen.getByText("Note B")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "批准：Shared demand" }));
    await waitFor(() => expect(review).toHaveBeenCalledWith("opp-1", { decision: "approve" }));
  });

  it("requires a rejection reason and blocks duplicate review requests", async () => {
    let resolve!: (value: typeof opportunity) => void;
    const review = vi.fn(() => new Promise<typeof opportunity>(done => { resolve = done; }));
    render(<OpportunitiesPage loadOpportunities={vi.fn().mockResolvedValue({ opportunities: [opportunity], accounts: [], evidence: [] })} reviewOpportunity={review} />);
    await screen.findByText("Shared demand");
    const reject = screen.getByRole("button", { name: "拒绝：Shared demand" });
    expect(reject).toBeDisabled();
    fireEvent.change(screen.getByLabelText("拒绝 Shared demand 的原因"), { target: { value: "No common demand" } });
    fireEvent.click(reject); fireEvent.click(reject);
    expect(review).toHaveBeenCalledTimes(1);
    expect(review).toHaveBeenCalledWith("opp-1", { decision: "reject", reason: "No common demand" });
    resolve(opportunity);
    await waitFor(() => expect(reject).toBeEnabled());
  });

  it("keeps API failure visible and retryable", async () => {
    render(<OpportunitiesPage loadOpportunities={vi.fn().mockRejectedValue(new Error("offline"))} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("无法加载机会候选");
  });
});
