import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./main";

describe("workbench shell", () => {
  it("renders the unified business navigation", () => {
    render(<App pathname="/status" />);

    expect(screen.getByRole("link", { name: "小红书 AI 工作台" })).toHaveAttribute("href", "/radar");
    expect(screen.getByRole("navigation", { name: "工作台导航" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "雷达总览" })).toHaveAttribute("href", "/radar");
    expect(screen.getByRole("link", { name: "机会审核" })).toHaveAttribute("href", "/opportunities");
    expect(screen.getByRole("link", { name: "内容工作台" })).toHaveAttribute("href", "/content");
    expect(screen.getByRole("link", { name: "任务记录" })).toHaveAttribute("href", "/jobs");
    expect(screen.getByRole("link", { name: "系统状态" })).toHaveAttribute("aria-current", "page");
  });

  it("keeps the account evidence route highlighted", () => {
    render(<App pathname="/accounts/demo-account" />);
    expect(screen.getByRole("link", { name: "账号证据" })).toHaveAttribute("aria-current", "page");
  });

  it("renders a truthful boundary note", () => {
    render(<App pathname="/jobs" />);
    expect(screen.getByRole("note")).toHaveTextContent("产品研究与产品制作按具体商品独立执行，不在本工作台自动化");
  });
});
