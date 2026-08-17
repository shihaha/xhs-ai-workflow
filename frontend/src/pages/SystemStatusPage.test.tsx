import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { HealthResponse } from "../api/client";
import { SystemStatusPage } from "./SystemStatusPage";

const healthFixture: HealthResponse = {
  status: "degraded",
  checks: {
    database: {
      healthy: true,
      path: "D:\\AI_WORKSPACE_RUNTIME\\xhs-intelligence-workbench\\workbench.sqlite3",
    },
    adb: { healthy: false, executable: "adb" },
    browser: { healthy: true, executable: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" },
    bailian: { healthy: false },
  },
};

describe("SystemStatusPage", () => {
  it("shows a factual loading state while health is being requested", () => {
    render(<SystemStatusPage loadHealth={() => new Promise<HealthResponse>(() => {})} />);

    expect(screen.getByRole("status")).toHaveTextContent("Loading system checks");
    expect(screen.getByLabelText("Loading system checks")).toHaveAttribute("aria-busy", "true");
  });

  it("renders every reported check and does not infer unavailable prerequisites", async () => {
    render(<SystemStatusPage loadHealth={vi.fn().mockResolvedValue(healthFixture)} />);

    expect(await screen.findByRole("heading", { name: "System status" })).toBeVisible();
    expect(screen.getByText("Degraded")).toBeVisible();
    expect(screen.getAllByText("Available")).toHaveLength(2);
    expect(screen.getAllByText("Unavailable")).toHaveLength(2);
    expect(screen.getByText(/workbench\.sqlite3/)).toBeVisible();
    expect(screen.getAllByText("adb")).toHaveLength(2);
    expect(screen.getByText(/Chrome\\Application\\chrome\.exe/)).toBeVisible();
  });

  it("explains a failed health request and provides a retry control", async () => {
    const loadHealth = vi.fn().mockRejectedValue(new Error("Network unavailable"));
    render(<SystemStatusPage loadHealth={loadHealth} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load system checks");
    expect(screen.getByRole("button", { name: "Retry system checks" })).toBeEnabled();
  });
});
