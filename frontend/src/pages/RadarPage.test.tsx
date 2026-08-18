import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RadarPage } from "./RadarPage";

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
});
