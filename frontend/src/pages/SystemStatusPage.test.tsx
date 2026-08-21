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

    expect(screen.getByRole("status")).toHaveTextContent("正在检查系统状态");
    expect(screen.getByLabelText("正在检查系统状态")).toHaveAttribute("aria-busy", "true");
  });

  it("renders every reported check and does not infer unavailable prerequisites", async () => {
    render(<SystemStatusPage loadHealth={vi.fn().mockResolvedValue(healthFixture)} />);

    expect(await screen.findByRole("heading", { name: "系统状态" })).toBeVisible();
    expect(screen.getByText("部分功能不可用")).toBeVisible();
    expect(screen.getAllByText("可用")).toHaveLength(2);
    expect(screen.getAllByText("不可用")).toHaveLength(2);
    expect(screen.getByText(/workbench\.sqlite3/)).toBeVisible();
    expect(screen.getByText("Android设备")).toBeVisible();
    expect(screen.getByText("百炼")).toBeVisible();
    expect(screen.getByText("adb")).toBeVisible();
    expect(screen.getByText(/Chrome\\Application\\chrome\.exe/)).toBeVisible();
  });

  it("explains a failed health request and provides a retry control", async () => {
    const loadHealth = vi.fn().mockRejectedValue(new Error("Network unavailable"));
    render(<SystemStatusPage loadHealth={loadHealth} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("无法加载系统状态");
    expect(screen.getByRole("button", { name: "重新检查" })).toBeEnabled();
  });
});
