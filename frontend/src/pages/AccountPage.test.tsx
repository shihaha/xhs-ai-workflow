import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AccountPage } from "./AccountPage";

const account = { user_id: "author-1", account_name: "真实账号", score: 4.25, evidence: 2.5, credibility: 1.25, accessibility: 1.36, fans: 400, gmv: "1万-10万", pay: "5%-10%", read: "1万-10万", nday: 3, nboard: 2 };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

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

  it("blocks duplicate account mutations while one request is pending", async () => {
    let resolve!: (value: { job_id: string; status: "queued" }) => void;
    const queueShop = vi.fn(() => new Promise<{ job_id: string; status: "queued" }>(done => { resolve = done; }));
    render(<AccountPage accountId="author-1" loadAccount={vi.fn().mockResolvedValue({ account, evidence: [], analyses: [], jobs: [], devices: [] })} queueShop={queueShop} />);
    await screen.findByRole("heading", { name: "真实账号" });
    const button = screen.getByRole("button", { name: "Queue device collection" });
    fireEvent.click(button); fireEvent.click(button);
    expect(queueShop).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    resolve({ job_id: "job-1", status: "queued" });
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("does not submit a second account collection while the first request is pending", async () => {
    let resolve!: (value: { job_id: string; status: "queued" }) => void;
    const startAccountCollection = vi.fn(() => new Promise<{ job_id: string; status: "queued" }>(done => { resolve = done; }));
    render(<AccountPage accountId="author-1" loadAccount={vi.fn().mockResolvedValue({ account, profile: null, notes: [], evidence: [], analyses: [], jobs: [], devices: [] })} startAccountCollection={startAccountCollection} pollIntervalMs={60_000} />);
    await screen.findByRole("heading", { name: "真实账号" });

    const start = screen.getByRole("button", { name: "Collect account and notes" });
    fireEvent.click(start); fireEvent.click(start);

    expect(startAccountCollection).toHaveBeenCalledTimes(1);
    expect(start).toBeDisabled();
    resolve({ job_id: "xhs-job-1", status: "queued" });
    await waitFor(() => expect(screen.getByText("xhs-job-1")).toBeVisible());
    expect(start).toBeDisabled();
  });

  it("shows needs-human detail and preserves the old job before retry", async () => {
    const startAccountCollection = vi.fn()
      .mockResolvedValueOnce({ job_id: "xhs-job-old", status: "queued" })
      .mockResolvedValueOnce({ job_id: "xhs-job-new", status: "queued" });
    const loadCollectionJob = vi.fn()
      .mockResolvedValueOnce({ id: "xhs-job-old", type: "xhs_account_collection", input: { user_id: "author-1", expected_note_count: 1 }, state: "needs_human", progress_current: 0, progress_total: 1, current_stage: "xhs_collection_result", error_category: "login_required", retry_count: 0, created_at: "2026-08-18T00:00:00Z", updated_at: "2026-08-18T00:00:01Z", started_at: null, completed_at: null, lease_expires_at: null, logs: [], artifacts: [] })
      .mockResolvedValueOnce({ id: "xhs-job-new", type: "xhs_account_collection", input: { user_id: "author-1", expected_note_count: 1 }, state: "succeeded", progress_current: 1, progress_total: 1, current_stage: "xhs_collection_result", error_category: null, retry_count: 0, created_at: "2026-08-18T00:01:00Z", updated_at: "2026-08-18T00:01:01Z", started_at: null, completed_at: null, lease_expires_at: null, logs: [], artifacts: [] });
    render(<AccountPage accountId="author-1" loadAccount={vi.fn().mockResolvedValue({ account, profile: null, notes: [], evidence: [], analyses: [], jobs: [], devices: [] })} startAccountCollection={startAccountCollection} loadCollectionJob={loadCollectionJob} pollIntervalMs={1} />);
    await screen.findByRole("heading", { name: "真实账号" });

    fireEvent.click(screen.getByRole("button", { name: "Collect account and notes" }));
    expect(await screen.findByText(/login_required/)).toBeVisible();
    expect(screen.getByText("xhs-job-old")).toBeVisible();
    expect(screen.getAllByText(/local xhs-cli session/i)).not.toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Collect account and notes" }));
    expect(await screen.findByText("xhs-job-new")).toBeVisible();
    expect(screen.getByText("xhs-job-old")).toBeVisible();
  });

  it("renders trusted profile, public note source links and canonical evidence ids without claiming opportunity eligibility", async () => {
    render(<AccountPage accountId="author-1" loadAccount={vi.fn().mockResolvedValue({
      account,
      profile: { user_id: "author-1", source_url: "https://www.xiaohongshu.com/user/profile/author-1", nickname: "真实账号资料", bio: "公开简介", public_stats: { followers_count: 400 }, collection_job_id: "xhs-job-1", collection_artifact_id: 7, collected_at: "2026-08-18T00:00:00Z" },
      notes: [{ note_id: "note-1", user_id: "author-1", source_url: "https://www.xiaohongshu.com/explore/note-1", title: "露营收纳笔记", summary: "公开摘要", published_at: "2026-08-18", public_interactions: { liked_count: 7 }, collection_job_id: "xhs-job-1", collection_artifact_id: 7, collected_at: "2026-08-18T00:00:00Z" }],
      evidence: [{ evidence_id: "account-note:17", kind: "account_note", account_user_id: "author-1", eligible_for_opportunity: true }],
      analyses: [], jobs: [], devices: [],
    })} />);

    expect(await screen.findByText("真实账号资料")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open note source" })).toHaveAttribute("href", "https://www.xiaohongshu.com/explore/note-1");
    expect(screen.getAllByText("account-note:17", { exact: false })).not.toHaveLength(0);
    expect(screen.getByText(/does not replace exact shop N\/N verification/i)).toBeVisible();
    expect(screen.queryByText(/notes make this opportunity eligible/i)).not.toBeInTheDocument();
  });

  it("surfaces a stale collection read and bounds automatic polling", async () => {
    const startAccountCollection = vi.fn().mockResolvedValue({ job_id: "xhs-job-stale", status: "queued" });
    const loadCollectionJob = vi.fn().mockRejectedValue(new Error("job read offline"));
    render(<AccountPage accountId="author-1" loadAccount={vi.fn().mockResolvedValue({ account, profile: null, notes: [], evidence: [], analyses: [], jobs: [], devices: [] })} startAccountCollection={startAccountCollection} loadCollectionJob={loadCollectionJob} pollIntervalMs={1} maxPolls={2} />);
    await screen.findByRole("heading", { name: "真实账号" });
    fireEvent.click(screen.getByRole("button", { name: "Collect account and notes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/job read offline.*stale/i);
    await waitFor(() => expect(loadCollectionJob).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/Automatic account refresh stopped after 2 checks/)).toBeVisible();
  });

  it("releases an exhausted account poll and can resume only that returned job", async () => {
    const firstRead = deferred<ReturnType<typeof accountJob>>();
    const startAccountCollection = vi.fn().mockResolvedValue({ job_id: "xhs-job-bounded", status: "queued" });
    const loadCollectionJob = vi.fn()
      .mockImplementationOnce(() => firstRead.promise)
      .mockResolvedValue(accountJob("xhs-job-bounded", "running"));
    render(<AccountPage accountId="author-1" loadAccount={vi.fn().mockResolvedValue({ account, profile: null, notes: [], evidence: [], analyses: [], jobs: [], devices: [] })} startAccountCollection={startAccountCollection} loadCollectionJob={loadCollectionJob} pollIntervalMs={100} maxPolls={2} />);
    await screen.findByRole("heading", { name: "真实账号" });
    vi.useFakeTimers();
    try {
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Collect account and notes" })); await Promise.resolve(); });
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(loadCollectionJob).toHaveBeenCalledWith("xhs-job-bounded");
      firstRead.resolve(accountJob("xhs-job-bounded", "running"));
      await act(async () => { await Promise.resolve(); });
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });

      expect(screen.getByText("Automatic account refresh stopped after 2 checks.")).toBeVisible();
      expect(screen.getByText("xhs-job-bounded")).toBeVisible();
      expect(screen.getByRole("button", { name: "Collect account and notes" })).toBeEnabled();
      const resume = screen.getByRole("button", { name: "Continue refreshing xhs-job-bounded" });
      expect(resume).toBeEnabled();

      await act(async () => { fireEvent.click(resume); await Promise.resolve(); });
      expect(screen.getByRole("button", { name: "Collect account and notes" })).toBeDisabled();
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(loadCollectionJob).toHaveBeenCalledTimes(3);
      expect(loadCollectionJob.mock.calls.every(([jobId]) => jobId === "xhs-job-bounded")).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("marks an ineligible account note as untrusted and prevents selecting it", async () => {
    const createAnalysis = vi.fn();
    render(<AccountPage accountId="author-1" loadAccount={vi.fn().mockResolvedValue({
      account,
      profile: null,
      notes: [],
      evidence: [{ evidence_id: "account-note:99", kind: "account_note", account_user_id: "author-1", eligible_for_opportunity: false }],
      analyses: [], jobs: [], devices: [],
    })} createAnalysis={createAnalysis} />);

    await screen.findByRole("heading", { name: "真实账号" });
    expect(screen.getByRole("heading", { name: "Notes requiring human verification" }).parentElement).toHaveTextContent(/account-note:99.*stale or untrusted.*human verification/i);
    expect(screen.queryByText(/account-note:99.*trusted account-note input/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText("account-note:99")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Generate account report" })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("account-note:99"));
    fireEvent.click(screen.getByRole("button", { name: "Generate account report" }));
    expect(createAnalysis).not.toHaveBeenCalled();
  });

  it("cancels scheduled account polling when the page unmounts", async () => {
    const startAccountCollection = vi.fn().mockResolvedValue({ job_id: "xhs-job-unmount", status: "queued" });
    const loadCollectionJob = vi.fn();
    const view = render(<AccountPage accountId="author-1" loadAccount={vi.fn().mockResolvedValue({ account, profile: null, notes: [], evidence: [], analyses: [], jobs: [], devices: [] })} startAccountCollection={startAccountCollection} loadCollectionJob={loadCollectionJob} pollIntervalMs={20} />);
    await screen.findByRole("heading", { name: "真实账号" });
    fireEvent.click(screen.getByRole("button", { name: "Collect account and notes" }));
    await waitFor(() => expect(startAccountCollection).toHaveBeenCalledTimes(1));
    view.unmount();
    await new Promise(resolve => window.setTimeout(resolve, 40));
    expect(loadCollectionJob).not.toHaveBeenCalled();
  });
});

function accountJob(id: string, state: "queued" | "running" | "needs_human" | "succeeded" | "failed" | "cancelled") {
  return {
    id, type: "xhs_account_collection", input: { user_id: "author-1", expected_note_count: 1 }, state,
    progress_current: state === "succeeded" ? 1 : 0, progress_total: 1, current_stage: "xhs_collection_result", error_category: null,
    retry_count: 0, created_at: "2026-08-18T00:00:00Z", updated_at: "2026-08-18T00:00:01Z", started_at: null, completed_at: null, lease_expires_at: null, logs: [], artifacts: [],
  } as const;
}
