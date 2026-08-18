import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./main";

describe("App workflow routes", () => {
  it("exposes every operator stage in navigation", () => {
    render(<App pathname="/unknown" />);
    expect(screen.getByRole("link", { name: "Radar" })).toHaveAttribute("href", "/radar");
    expect(screen.getByRole("link", { name: "Opportunities" })).toHaveAttribute("href", "/opportunities");
    expect(screen.getByRole("link", { name: "Content studio" })).toHaveAttribute("href", "/content");
  });

  it("routes encoded account identities to the account workbench", () => {
    render(<App pathname="/accounts/author-1" />);
    expect(screen.getByRole("link", { name: "Accounts" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByText("Route not found")).not.toBeInTheDocument();
  });
});
