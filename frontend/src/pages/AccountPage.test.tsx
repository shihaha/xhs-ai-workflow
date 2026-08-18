import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AccountPage } from "./AccountPage";

const account = { user_id: "author-1", account_name: "真实账号", score: 4.25, evidence: 2.5, credibility: 1.25, accessibility: 1.36, fans: 400, gmv: "1万-10万", pay: "5%-10%", read: "1万-10万", nday: 3, nboard: 2 };

describe("AccountPage", () => {
  it("shows missing account facts without inventing a profile", async () => {
    render(<AccountPage accountId="missing" loadAccount={vi.fn().mockResolvedValue({ account: null, evidence: [], analyses: [], jobs: [], devices: [] })} />);
    expect(await screen.findByText("Account not found in persisted ranking evidence")).toBeVisible();
  });

  it("surfaces unavailable device and a needs-human collection with evidence", async () => {
    render(<AccountPage accountId="author-1" loadAccount={vi.fn().mockResolvedValue({
      account,
      evidence: [{ evidence_id: "rank-item:7", kind: "rank_item", account_user_id: "author-1", eligible_for_opportunity: true }],
      analyses: [],
      devices: [{ status: "unavailable", device_id: null, detail: "adb_unavailable", raw_evidence: { adb_executable: "adb" } }],
      jobs: [{ id: "job-1", type: "android_shop_collection", input: { account_user_id: "author-1", expected_count: 8 }, state: "needs_human", progress_current: 3, progress_total: 8, current_stage: "login_confirmation", error_category: "login_required", retry_count: 0, created_at: "2026-08-17T08:00:00", updated_at: "2026-08-17T08:01:00", started_at: null, completed_at: null, lease_expires_at: null, logs: [], artifacts: [{ kind: "shop_collection_result", path: "evidence/android/job-1/result.json", metadata: { result: { missing_items: [{ reference: "expected_product:4", reason: "login_interrupted" }], missing_count: 5, expected_count: 8, succeeded_count: 3 } } }] }],
    })} />);

    expect(await screen.findByRole("heading", { name: "真实账号" })).toBeVisible();
    expect(screen.getByText("Device unavailable: adb_unavailable")).toBeVisible();
    expect(screen.getByText("Human attention required")).toBeVisible();
    expect(screen.getByText("3 / 8 verified or collected")).toBeVisible();
    expect(screen.getByText("expected_product:4 · login_interrupted")).toBeVisible();
    expect(screen.getByRole("link", { name: "Inspect 1 evidence item" })).toHaveAttribute("href", "/jobs#job-job-1-evidence");
    expect(screen.getByText(/Resolve the device, login, or evidence issue/)).toBeVisible();
  });

  it("renders persisted account analysis claims, evidence and failure guidance", async () => {
    render(<AccountPage accountId="author-1" loadAccount={vi.fn().mockResolvedValue({
      account, evidence: [], devices: [], jobs: [], analyses: [
        { id: "analysis-ok", analysis_type: "account_report", account_user_id: "author-1", account_user_ids: [], status: "succeeded", evidence_ids: ["rank-item:7"], output: { claims: [{ claim: "账号需求信号明确", evidence_ids: ["rank-item:7"] }] }, error_category: null, error_detail: null },
        { id: "analysis-fail", analysis_type: "account_report", account_user_id: "author-1", account_user_ids: [], status: "needs_human", evidence_ids: ["artifact:3"], output: null, error_category: "deep_verification_incomplete", error_detail: "Complete shop verification is required." },
      ],
    })} />);

    expect(await screen.findByText("账号需求信号明确")).toBeVisible();
    expect(screen.getByText("Evidence: rank-item:7")).toBeVisible();
    expect(screen.getByText("Complete shop verification is required.")).toBeVisible();
    expect(screen.getByText(/Resolve the reported evidence or provider issue/)).toBeVisible();
  });

  it("queues a real device job and requests an evidence-bound analysis", async () => {
    const queueShop = vi.fn().mockResolvedValue({ job_id: "job-new", status: "queued" });
    const createAnalysis = vi.fn().mockResolvedValue({ id: "analysis-1", status: "succeeded" });
    const loadAccount = vi.fn().mockResolvedValue({
      account,
      evidence: [{ evidence_id: "rank-item:7", kind: "rank_item", account_user_id: "author-1", eligible_for_opportunity: true }],
      analyses: [], jobs: [], devices: [{ status: "available", device_id: "serial-1", detail: "ready", raw_evidence: {} }],
    });
    render(<AccountPage accountId="author-1" loadAccount={loadAccount} queueShop={queueShop} createAnalysis={createAnalysis} />);

    await screen.findByRole("heading", { name: "真实账号" });
    fireEvent.change(screen.getByLabelText("Expected shop products"), { target: { value: "6" } });
    fireEvent.change(screen.getByLabelText("Verification evidence directory"), { target: { value: "evidence/e2e-shop" } });
    fireEvent.click(screen.getByRole("button", { name: "Queue device collection" }));
    await waitFor(() => expect(queueShop).toHaveBeenCalledWith(expect.objectContaining({ account_user_id: "author-1", expected_count: 6, device_id: "serial-1", verification_dir: "evidence/e2e-shop" })));
    expect(await screen.findByText("Queued job job-new")).toBeVisible();
    await waitFor(() => expect(loadAccount).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByLabelText("rank-item:7"));
    fireEvent.click(screen.getByRole("button", { name: "Generate account report" }));
    await waitFor(() => expect(createAnalysis).toHaveBeenCalledWith({ analysis_type: "account_report", account_user_id: "author-1", account_user_ids: [], evidence_ids: ["rank-item:7"] }));

    fireEvent.click(screen.getByRole("button", { name: "Generate opportunity analysis" }));
    await waitFor(() => expect(createAnalysis).toHaveBeenCalledWith({ analysis_type: "account_opportunity", account_user_ids: ["author-1"], evidence_ids: ["rank-item:7"] }));
  });
});
