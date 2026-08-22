import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ContentResearchPage } from "./ContentResearchPage";
import type { FinishedProductDossier, KeywordPlan } from "../api/contentResearch";

const dossier: FinishedProductDossier = {
  id: "dossier-1",
  product_key: "seven-sins-test",
  name: "七宗罪测试",
  version: "1.0.0",
  target_user: "希望了解自身性格倾向的用户",
  core_need: "低门槛获得可理解的人格测试结果",
  deliverables: ["测试题", "结果解释"],
  usage_instructions: "按题作答后查看结果。",
  faq: [],
  allowed_claims: ["提供七宗罪主题的人格测试体验"],
  forbidden_claims: ["用于心理疾病诊断"],
  source_index: ["产品说明.md"],
  uat_status: "passed",
  created_at: "2026-08-23T00:00:00Z",
};

const emptyPlan: KeywordPlan = {
  dossier_id: dossier.id,
  run_id: null,
  source: null,
  provider: null,
  model: null,
  prompt_version: null,
  usage: {},
  duration_ms: null,
  created_at: null,
  count: 0,
  items: [],
};

const generatedPlan: KeywordPlan = {
  dossier_id: dossier.id,
  run_id: "run-1",
  source: "ai",
  provider: "alibaba_bailian",
  model: "deepseek-v4-flash",
  prompt_version: "tutorial-content-keyword-layout-v1",
  usage: { total_tokens: 120 },
  duration_ms: 42,
  created_at: "2026-08-23T00:01:00Z",
  count: 10,
  items: Array.from({ length: 10 }, (_, index) => ({
    id: `keyword-${index + 1}`,
    run_id: "run-1",
    position: index + 1,
    keyword: `七宗罪测试-${index + 1}`,
    category: index < 2 ? "main" : "audience",
    expand: index === 0,
    scope: "benchmark",
    target_count: 10,
    created_at: "2026-08-23T00:01:00Z",
  })),
};

describe("ContentResearchPage", () => {
  it("keeps product research and product making outside the workbench", async () => {
    render(
      <ContentResearchPage
        loadResearch={async () => ({ dossiers: [], keywordPlans: {} })}
      />,
    );

    expect(await screen.findByRole("heading", { name: "成品资料" })).toBeInTheDocument();
    expect(screen.getByText(/产品研究与产品制作不在本工作台自动化/)).toBeInTheDocument();
    expect(screen.getByText(/还没有完成 UAT 的成品资料/)).toBeInTheDocument();
  });

  it("generates a tutorial-bounded keyword network and reloads persisted facts", async () => {
    let loadedPlan = emptyPlan;
    const generate = vi.fn(async () => {
      loadedPlan = generatedPlan;
      return generatedPlan;
    });
    const load = vi.fn(async () => ({
      dossiers: [dossier],
      keywordPlans: { [dossier.id]: loadedPlan },
    }));

    render(
      <ContentResearchPage
        loadResearch={load}
        generateKeywordPlan={generate}
      />,
    );

    const button = await screen.findByRole("button", { name: "AI 生成关键词网络" });
    fireEvent.click(button);

    await waitFor(() => expect(generate).toHaveBeenCalledWith(dossier.id));
    expect(await screen.findByText(/已生成关键词网络：七宗罪测试 · 10 个词/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新生成关键词网络" })).toBeInTheDocument();
    expect(screen.getByText("七宗罪测试-1")).toBeInTheDocument();
    expect(screen.getByText(/deepseek-v4-flash/)).toBeInTheDocument();
    expect(screen.getByText(/tutorial-content-keyword-layout-v1/)).toBeInTheDocument();
  });

  it("surfaces model failure without inventing a keyword plan", async () => {
    const generate = vi.fn(async () => {
      throw new Error("Bailian is not configured.");
    });

    render(
      <ContentResearchPage
        loadResearch={async () => ({
          dossiers: [dossier],
          keywordPlans: { [dossier.id]: emptyPlan },
        })}
        generateKeywordPlan={generate}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "AI 生成关键词网络" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Bailian is not configured.");
    expect(screen.getByText(/还没有关键词网络/)).toBeInTheDocument();
  });
});
