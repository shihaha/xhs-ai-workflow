import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Job, JobListResponse } from "../api/client";
import { JobsPage } from "./JobsPage";

const baseJob: Job = {
  id: "d7172492-466c-4d7d-9b5e-a12c127899c4",
  type: "shop_collection",
  input: { account_id: "xhs_219" },
  state: "queued",
  progress_current: 0,
  progress_total: null,
  current_stage: null,
  error_category: null,
  retry_count: 0,
  created_at: "2026-08-17T09:00:00",
  updated_at: "2026-08-17T09:00:00",
  started_at: null,
  completed_at: null,
  lease_expires_at: null,
  logs: [],
  artifacts: [],
};

describe("JobsPage", () => {
  it("shows a factual loading state while jobs are being requested", () => {
    render(<JobsPage loadJobs={() => new Promise<JobListResponse>(() => {})} />);

    expect(screen.getByRole("status")).toHaveTextContent("Loading jobs");
    expect(screen.getByLabelText("Loading jobs")).toHaveAttribute("aria-busy", "true");
  });

  it("explains the real empty response without creating example jobs", async () => {
    render(<JobsPage loadJobs={vi.fn().mockResolvedValue([])} />);

    expect(await screen.findByText("No jobs recorded")).toBeVisible();
    expect(screen.queryByText("shop_collection")).not.toBeInTheDocument();
  });

  it("makes a failed job and its reported error category visible", async () => {
    const failedJob: Job = {
      ...baseJob,
      state: "failed",
      progress_current: 3,
      progress_total: 8,
      current_stage: "extracting notes",
      error_category: "network_timeout",
      completed_at: "2026-08-17T09:04:00",
      logs: [{ level: "error", message: "Remote endpoint timed out." }],
    };
    render(<JobsPage loadJobs={vi.fn().mockResolvedValue([failedJob])} />);

    expect(await screen.findByText("Failed")).toBeVisible();
    expect(screen.getByText("Error category: network_timeout")).toBeVisible();
    expect(screen.getByText("3 / 8 reported")).toBeVisible();
    expect(screen.getByRole("region", { name: "Job logs" })).toHaveTextContent("Remote endpoint timed out.");
  });

  it("identifies jobs that need a person without presenting them as completed", async () => {
    const reviewJob: Job = {
      ...baseJob,
      state: "needs_human",
      current_stage: "login confirmation",
      retry_count: 2,
      started_at: "2026-08-17T09:01:00",
      logs: [{ level: "warning", message: "Login prompt detected on the device." }],
    };
    render(<JobsPage loadJobs={vi.fn().mockResolvedValue([reviewJob])} />);

    expect(await screen.findByText("Human attention required")).toBeVisible();
    expect(screen.getByText("Current stage: login confirmation")).toBeVisible();
    expect(screen.getByText("Retries: 2")).toBeVisible();
    expect(screen.queryByText("Completed")).not.toBeInTheDocument();
  });

  it("exposes evidence paths attached to a job", async () => {
    const evidenceJob: Job = {
      ...baseJob,
      state: "succeeded",
      progress_current: 1,
      progress_total: 1,
      current_stage: "saved evidence",
      completed_at: "2026-08-17T09:03:00",
      artifacts: [
        {
          kind: "screenshot",
          path: "artifacts/d7172492/screen-01.png",
          metadata: { source_url: "https://www.xiaohongshu.com/explore/example" },
        },
      ],
    };
    render(<JobsPage loadJobs={vi.fn().mockResolvedValue([evidenceJob])} />);

    expect(await screen.findByRole("link", { name: "1 evidence item" })).toHaveAttribute(
      "href",
      "#job-d7172492-466c-4d7d-9b5e-a12c127899c4-evidence",
    );
    expect(screen.getByText("artifacts/d7172492/screen-01.png")).toBeVisible();
    expect(screen.getByText("screenshot")).toBeVisible();
  });

  it("explains a failed jobs request and provides a retry control", async () => {
    render(<JobsPage loadJobs={vi.fn().mockRejectedValue(new Error("Service unavailable"))} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load jobs");
    expect(screen.getByRole("button", { name: "Retry jobs" })).toBeEnabled();
  });
});
