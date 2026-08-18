import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
      accounts: [{ user_id: "author-1", account_name: "真实账号", score: 4.25, evidence: 2.5, credibility: 1.25, accessibility: 1.36, fans: 400, gmv: "1万-10万", pay: "5%-10%", read: "1万-10万", nday: 3, nboard: 2 }],
    })} />);

    expect(await screen.findByRole("heading", { name: "Demand radar" })).toBeVisible();
    expect(screen.getByText("热卖榜 · 优秀账号")).toBeVisible();
    expect(screen.getByRole("link", { name: "真实账号" })).toHaveAttribute("href", "/accounts/author-1");
    expect(screen.getByText("4.25")).toBeVisible();
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
});
