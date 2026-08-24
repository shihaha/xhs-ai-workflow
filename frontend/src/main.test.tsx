import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./main";

describe("App workflow routes", () => {
  it("exposes every operator stage in navigation", () => {
    render(<App pathname="/unknown" />);
    expect(screen.getByRole("link", { name: "需求雷达" })).toHaveAttribute("href", "/opportunities");
    expect(screen.getByRole("link", { name: "采集控制" })).toHaveAttribute("href", "/radar");
    expect(screen.getByRole("link", { name: "内容工作台" })).toHaveAttribute("href", "/content");
    expect(screen.getByRole("link", { name: "Agent 工作台" })).toHaveAttribute("href", "/agent");
  });

  it("routes encoded account identities to the account workbench", () => {
    render(<App pathname="/accounts/author-1" />);
    expect(screen.getByRole("link", { name: "账号证据" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByText("页面不存在")).not.toBeInTheDocument();
  });
});
