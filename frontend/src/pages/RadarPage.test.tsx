import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RadarPage } from "./RadarPage";

const scopes = ["阅读榜-优秀内容", "阅读榜-优秀账号", "引流榜-优秀内容", "引流榜-优秀账号", "热卖榜-优秀内容", "热卖榜-优秀账号", "成交榜-优秀内容", "成交榜-优秀账号"].map((value, index) => {
  const [board, dimension] = value.split("-");
  return { job_id: `scope-${index + 1}`, board, dimension, status: "queued" as const };
});
const job = (index: number, state: "queued" | "running" | "needs_human" | "succeeded" | "failed" | "cancelled") => ({
  id: `scope-${index + 1}`, type: "qianfan_ranking_scope", input: { board: scopes[index].board, dimension: scopes[index].dimension, selector_profile_version: "qianfan-note-rank-unverified-v1" }, state,
  progress_current: state === "succeeded" ? 1 : 0, progress_total: 1, current_stage: state, error_category: state === "needs_human" ? "selector_profile_unverified" : null,
  retry_count: 0, created_at: "2026-08-18T00:00:00Z", updated_at: "2026-08-18T00:00:00Z", started_at: null, completed_at: null, lease_expires_at: null, logs: [], artifacts: [],
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

const searchJob = (id: string, state: "queued" | "running" | "needs_human" | "succeeded" | "failed" | "cancelled") => ({
  id, type: "xhs_note_search", input: { keyword: "收纳", expected_count: 1 }, state,
  progress_current: state === "succeeded" ? 1 : 0, progress_total: 1, current_stage: "xhs_collection_result", error_category: null,
  retry_count: 0, created_at: "2026-08-18T00:00:00Z", updated_at: "2026-08-18T00:00:01Z", started_at: null, completed_at: null, lease_expires_at: null, logs: [], artifacts: [],
} as const);

describe("RadarPage", () => {
  it("shows loading and then a truthful empty radar", async () => {
    render(<RadarPage loadRadar={vi.fn().mockResolvedValue({ snapshots: [], accounts: [] })} />);

    expect(screen.getByRole("status")).toHaveTextContent("Loading demand radar");
    expect(await screen.findByText("No ranking evidence recorded")).toBeVisible();
    expect(screen.queryByText(/example account/i)).not.toBeInTheDocument();
  });

  it("renders persisted ranking and scored-account facts with an account route", async () => {
    render(<RadarPage loadRadar={vi.fn().mockResolvedValue({
      snapshots: [{ id: 4, source_date: "2026-08-17", collected_at: "2026-08-17T08:00:00", board: "热卖榜", dimension: "优秀账号", source_url: "https://qianfan.example/rank", raw_evidence: { response: "saved" }, submitted_count: 2, deduplicated_count: 1, items: [] }],
      accounts: [{ user_id: "author-1", account_name: "真实账号", score: 4.25, score_status: "scored", ranking_evidence_count: 3, best_rank: 1, evidence: 2.5, credibility: 1.25, accessibility: 1.36, fans: 400, gmv: "1万-10万", pay: "5%-10%", read: "1万-10万", nday: 3, nboard: 2 }],
    })} />);

    expect(await screen.findByRole("heading", { name: "Demand radar" })).toBeVisible();
    expect(screen.getByText("热卖榜 · 优秀账号")).toBeVisible();
    expect(screen.getByRole("link", { name: "真实账号" })).toHaveAttribute("href", "/accounts/author-1");
    expect(screen.getByText("4.25")).toBeVisible();
  });

  it("shows missing score inputs without inventing a numeric score", async () => {
    render(<RadarPage loadRadar={vi.fn().mockResolvedValue({ snapshots: [], accounts: [{ user_id: "author-2", account_name: "缺指标账号", score: null, score_status: "insufficient_metrics", ranking_evidence_count: 4, best_rank: 2, evidence: 0, credibility: 0, accessibility: 0, fans: 0, gmv: "—", pay: "—", read: "—", nday: 2, nboard: 2 }] })} />);
    expect(await screen.findByText("insufficient_metrics")).toBeVisible();
    expect(screen.getByText(/4 appearances · best rank 2/)).toBeVisible();
  });

  it("shows an actionable API error instead of stale data", async () => {
    render(<RadarPage loadRadar={vi.fn().mockRejectedValue(new Error("offline"))} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load demand radar");
    expect(screen.getByRole("button", { name: "Retry demand radar" })).toBeEnabled();
  });

  it("submits operator-captured snapshot JSON through the persisted API", async () => {
    const ingestSnapshot = vi.fn().mockResolvedValue({ id: 1 });
    const loadRadar = vi.fn().mockResolvedValue({ snapshots: [], accounts: [] });
    render(<RadarPage loadRadar={loadRadar} ingestSnapshot={ingestSnapshot} />);
    await screen.findByText("No ranking evidence recorded");
    fireEvent.change(screen.getByLabelText("Captured ranking snapshot JSON"), { target: { value: '{"source_date":"2026-08-17","items":[]}' } });
    fireEvent.click(screen.getByRole("button", { name: "Import captured snapshot" }));
    await waitFor(() => expect(ingestSnapshot).toHaveBeenCalledWith({ source_date: "2026-08-17", items: [] }));
    await waitFor(() => expect(loadRadar).toHaveBeenCalledTimes(2));
    expect(screen.getByText(/Snapshot 1 persisted/)).toBeVisible();
  });

  it("starts the strict eight-scope collection and never calls 7/8 complete", async () => {
    const startCollection = vi.fn().mockResolvedValue({ collection_id: "collection-1", scopes });
    const loadCollectionJobs = vi.fn().mockResolvedValue([
      ...Array.from({ length: 7 }, (_, index) => job(index, "succeeded")), job(7, "needs_human"),
    ]);
    render(<RadarPage loadRadar={vi.fn().mockResolvedValue({ snapshots: [], accounts: [], health: { status: "degraded", checks: { browser: { healthy: false, executable: null } } } })} startCollection={startCollection} loadCollectionJobs={loadCollectionJobs} pollIntervalMs={60_000} />);
    await screen.findByText("No ranking evidence recorded");
    fireEvent.change(screen.getByLabelText("Expected rows per ranking scope"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Start automatic Qianfan collection" }));
    await waitFor(() => expect(startCollection).toHaveBeenCalledWith({ expected_count_per_scope: 1 }));
    expect(await screen.findByText("Collection collection-1")).toBeVisible();
    await waitFor(() => expect(screen.getByText(/Not complete: 7\/8 scopes succeeded/)).toBeVisible());
    expect(screen.queryByText(/Complete: 8\/8/)).not.toBeInTheDocument();
    expect(screen.getByText(/qianfan-note-rank-unverified-v1/)).toBeVisible();
    expect(screen.getByText(/Profile status: unverified/)).toBeVisible();
    expect(screen.getByText(/Browser executable unavailable/)).toBeVisible();
  });

  it("surfaces a scheduled job refresh failure without an unhandled rejection", async () => {
    const startCollection = vi.fn().mockResolvedValue({ collection_id: "collection-1", scopes });
    const loadCollectionJobs = vi.fn().mockResolvedValueOnce([]).mockRejectedValueOnce(new Error("jobs offline"));
    render(<RadarPage loadRadar={vi.fn().mockResolvedValue({ snapshots: [], accounts: [] })} startCollection={startCollection} loadCollectionJobs={loadCollectionJobs} pollIntervalMs={1} />);
    await screen.findByText("No ranking evidence recorded");
    fireEvent.click(screen.getByRole("button", { name: "Start automatic Qianfan collection" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("jobs offline");
    expect(screen.getByRole("button", { name: "Refresh eight scope jobs" })).toBeEnabled();
  });

  it("prevents a second collection or import mutation while the first is pending", async () => {
    let resolve!: (value: { collection_id: string; scopes: typeof scopes }) => void;
    const startCollection = vi.fn(() => new Promise<{ collection_id: string; scopes: typeof scopes }>(done => { resolve = done; }));
    const ingestSnapshot = vi.fn().mockResolvedValue({ id: 1 });
    render(<RadarPage loadRadar={vi.fn().mockResolvedValue({ snapshots: [], accounts: [] })} startCollection={startCollection} ingestSnapshot={ingestSnapshot} pollIntervalMs={60_000} />);
    await screen.findByText("No ranking evidence recorded");
    const start = screen.getByRole("button", { name: "Start automatic Qianfan collection" });
    fireEvent.click(start); fireEvent.click(start);
    expect(startCollection).toHaveBeenCalledTimes(1);
    expect(start).toBeDisabled();
    expect(screen.getByRole("button", { name: "Import captured snapshot" })).toBeDisabled();
    resolve({ collection_id: "collection-1", scopes });
    await waitFor(() => expect(start).toBeEnabled());
  });

  it("starts keyword note search once and renders only normalized public results", async () => {
    let resolve!: (value: { job_id: string; status: "queued" }) => void;
    const startNoteSearch = vi.fn(() => new Promise<{ job_id: string; status: "queued" }>(done => { resolve = done; }));
    const loadSearchJob = vi.fn().mockResolvedValue({ id: "search-job-1", type: "xhs_note_search", input: { keyword: "露营收纳", expected_count: 1 }, state: "succeeded", progress_current: 1, progress_total: 1, current_stage: "xhs_collection_result", error_category: null, retry_count: 0, created_at: "2026-08-18T00:00:00Z", updated_at: "2026-08-18T00:00:01Z", started_at: null, completed_at: null, lease_expires_at: null, logs: [], artifacts: [] });
    const loadSearchResults = vi.fn().mockResolvedValue({ job_id: "search-job-1", keyword: "露营收纳", expected_count: 1, succeeded_count: 1, artifact_id: 9, collected_at: "2026-08-18T00:00:01Z", items: [{ note_id: "note-1", source_url: "https://www.xiaohongshu.com/explore/note-1", title: "露营装备收纳", summary: "公开摘要", user_id: "author-1" }] });
    render(<RadarPage loadRadar={vi.fn().mockResolvedValue({ snapshots: [], accounts: [] })} startNoteSearch={startNoteSearch} loadSearchJob={loadSearchJob} loadSearchResults={loadSearchResults} pollIntervalMs={1} />);
    await screen.findByText("No ranking evidence recorded");
    fireEvent.change(screen.getByLabelText("Note search keyword"), { target: { value: "露营收纳" } });
    const button = screen.getByRole("button", { name: "Search public notes" });
    fireEvent.click(button); fireEvent.click(button);
    expect(startNoteSearch).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    resolve({ job_id: "search-job-1", status: "queued" });

    expect(await screen.findByText("露营装备收纳")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open search result" })).toHaveAttribute("href", "https://www.xiaohongshu.com/explore/note-1");
    expect(screen.getByText("1 / 1 public notes returned")).toBeVisible();
    expect(screen.queryByText(/artifact_id|evidence\/xhs\/|secret-sentinel/i)).not.toBeInTheDocument();
  });

  it("shows needs-human search audit and stale polling errors without discarding the old job", async () => {
    const startNoteSearch = vi.fn()
      .mockResolvedValueOnce({ job_id: "search-old", status: "queued" })
      .mockResolvedValueOnce({ job_id: "search-new", status: "queued" });
    const loadSearchJob = vi.fn()
      .mockResolvedValueOnce({ id: "search-old", type: "xhs_note_search", input: { keyword: "收纳", expected_count: 1 }, state: "needs_human", progress_current: 0, progress_total: 1, current_stage: "xhs_collection_result", error_category: "captcha_required", retry_count: 0, created_at: "2026-08-18T00:00:00Z", updated_at: "2026-08-18T00:00:01Z", started_at: null, completed_at: null, lease_expires_at: null, logs: [], artifacts: [] })
      .mockRejectedValue(new Error("search job read offline"));
    render(<RadarPage loadRadar={vi.fn().mockResolvedValue({ snapshots: [], accounts: [] })} startNoteSearch={startNoteSearch} loadSearchJob={loadSearchJob} pollIntervalMs={1} searchMaxPolls={1} />);
    await screen.findByText("No ranking evidence recorded");
    fireEvent.change(screen.getByLabelText("Note search keyword"), { target: { value: "收纳" } });
    fireEvent.click(screen.getByRole("button", { name: "Search public notes" }));
    expect(await screen.findByText(/captcha_required/)).toBeVisible();
    expect(screen.getByText("search-old")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Search public notes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/search job read offline.*stale/i);
    expect(screen.getByText("search-old")).toBeVisible();
    expect(screen.getByText("search-new")).toBeVisible();
  });

  it("releases an exhausted search poll and can resume only that returned job", async () => {
    const firstRead = deferred<ReturnType<typeof searchJob>>();
    const startNoteSearch = vi.fn().mockResolvedValue({ job_id: "search-bounded", status: "queued" });
    const loadSearchJob = vi.fn()
      .mockImplementationOnce(() => firstRead.promise)
      .mockResolvedValue(searchJob("search-bounded", "running"));
    render(<RadarPage loadRadar={vi.fn().mockResolvedValue({ snapshots: [], accounts: [] })} startNoteSearch={startNoteSearch} loadSearchJob={loadSearchJob} pollIntervalMs={100} searchMaxPolls={2} />);
    await screen.findByText("No ranking evidence recorded");
    fireEvent.change(screen.getByLabelText("Note search keyword"), { target: { value: "收纳" } });
    vi.useFakeTimers();
    try {
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Search public notes" })); await Promise.resolve(); });
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(loadSearchJob).toHaveBeenCalledWith("search-bounded");
      firstRead.resolve(searchJob("search-bounded", "running"));
      await act(async () => { await Promise.resolve(); });
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });

      expect(screen.getByText("Automatic search refresh stopped after 2 checks.")).toBeVisible();
      expect(screen.getByText("search-bounded")).toBeVisible();
      expect(screen.getByRole("button", { name: "Search public notes" })).toBeEnabled();
      const resume = screen.getByRole("button", { name: "Continue refreshing search-bounded" });
      expect(resume).toBeEnabled();

      await act(async () => { fireEvent.click(resume); await Promise.resolve(); });
      expect(screen.getByRole("button", { name: "Search public notes" })).toBeDisabled();
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(loadSearchJob).toHaveBeenCalledTimes(3);
      expect(loadSearchJob.mock.calls.every(([jobId]) => jobId === "search-bounded")).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels scheduled search polling when the page unmounts", async () => {
    const startNoteSearch = vi.fn().mockResolvedValue({ job_id: "search-unmount", status: "queued" });
    const loadSearchJob = vi.fn();
    const view = render(<RadarPage loadRadar={vi.fn().mockResolvedValue({ snapshots: [], accounts: [] })} startNoteSearch={startNoteSearch} loadSearchJob={loadSearchJob} pollIntervalMs={20} />);
    await screen.findByText("No ranking evidence recorded");
    fireEvent.change(screen.getByLabelText("Note search keyword"), { target: { value: "收纳" } });
    fireEvent.click(screen.getByRole("button", { name: "Search public notes" }));
    await waitFor(() => expect(startNoteSearch).toHaveBeenCalledTimes(1));
    view.unmount();
    await new Promise(resolve => window.setTimeout(resolve, 40));
    expect(loadSearchJob).not.toHaveBeenCalled();
  });
});
