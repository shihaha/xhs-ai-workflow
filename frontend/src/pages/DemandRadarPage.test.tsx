import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { DemandRadar, DemandRadarDirection } from "../api/client";
import { DemandRadarPage } from "./DemandRadarPage";

const direction = (overrides: Partial<DemandRadarDirection> = {}): DemandRadarDirection => ({
  opportunity_id: "opp-1",
  title: "七宗罪人格测试",
  summary: "多个独立账号反复销售相近的人格测试产品。",
  evidence_level: "validated_candidate",
  review_status: "pending_review",
  can_follow_up: true,
  journey_stage: "demand_decision",
  is_new_today: true,
  supporting_account_count: 3,
  supporting_product_count: 8,
  supporting_note_count: 5,
  image_evidence_count: 21,
  representative_products: [
    {
      account_user_id: "account-a",
      product_id: "product-a",
      title: "七宗罪人格测试完整版",
      source_url: "https://www.xiaohongshu.com/goods/product-a",
      price: "¥19.9",
      sold: "1.2万+",
      image_evidence_count: 6,
      image_artifact_ids: [21, 22],
    },
    {
      account_user_id: "account-b",
      product_id: "product-b",
      title: "黑暗人格测试报告",
      source_url: "https://www.xiaohongshu.com/goods/product-b",
      price: "¥29.9",
      sold: "8300+",
      image_evidence_count: 5,
      image_artifact_ids: [],
    },
  ],
  linked_products: [],
  next_business_action: "查看支撑账号、商品和图片证据后，决定跟进、继续观察或拒绝。",
  created_at: "2026-08-24T10:00:00",
  reviewed_at: null,
  ...overrides,
});

const radar = (directions: DemandRadarDirection[] = [direction()]): DemandRadar => ({
  summary: {
    generated_on: "2026-08-24",
    total_direction_count: directions.length,
    new_today_count: directions.filter(item => item.is_new_today).length,
    warming_count: directions.filter(item => item.evidence_level === "warming_candidate" && item.review_status !== "rejected").length,
    validated_count: directions.filter(item => item.evidence_level === "validated_candidate" && item.review_status !== "rejected").length,
    pending_decision_count: directions.filter(item => item.review_status === "pending_review").length,
    approved_count: directions.filter(item => item.review_status === "approved").length,
    linked_product_count: directions.reduce((total, item) => total + item.linked_products.length, 0),
  },
  directions,
});

describe("DemandRadarPage", () => {
  it("renders tutorial-aligned business summary and representative product evidence", async () => {
    render(<DemandRadarPage loadDemandRadar={vi.fn().mockResolvedValue(radar())} />);

    expect(await screen.findByRole("heading", { name: "需求雷达" })).toBeVisible();
    const overview = screen.getByRole("region", { name: "需求雷达概览" });
    expect(within(overview).getByText("今日新增")).toBeVisible();
    expect(within(overview).getByText("待你决定")).toBeVisible();
    expect(screen.getByText("七宗罪人格测试完整版")).toBeVisible();
    expect(screen.getByText("价格 ¥19.9")).toBeVisible();
    expect(screen.getByText("销量 1.2万+")).toBeVisible();
    const firstImage = screen.getByAltText("七宗罪人格测试完整版 证据图 1");
    expect(firstImage).toHaveAttribute("src", "/api/v1/business/demand-radar/opp-1/media/21");
    expect(screen.getByText("2 张已通过安全 Artifact 绑定，可逐张查看")).toBeVisible();
    expect(screen.getByLabelText("黑暗人格测试报告 的图片证据摘要")).toHaveTextContent("5份图片证据当前记录暂没有可验证的安全预览绑定");
    fireEvent.click(screen.getByRole("button", { name: "放大查看七宗罪人格测试完整版 证据图 1" }));
    const lightbox = screen.getByRole("dialog", { name: "商品证据大图" });
    expect(within(lightbox).getByAltText("七宗罪人格测试完整版 证据图 1")).toHaveAttribute("src", "/api/v1/business/demand-radar/opp-1/media/21");
    fireEvent.click(within(lightbox).getByRole("button", { name: "关闭大图" }));
    expect(screen.queryByRole("dialog", { name: "商品证据大图" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("查看支撑证据"));
    expect(screen.getByText(/浏览器不会获得本机 Artifact 路径/)).toBeVisible();
    expect(screen.getByRole("link", { name: "高级：运行跨账号需求分析" })).toHaveAttribute("href", "/opportunities/analysis");
  });

  it("approves a persisted positive direction once and reloads the business projection", async () => {
    const first = radar();
    const approvedDirection = direction({
      review_status: "approved",
      journey_stage: "product_definition",
      reviewed_at: "2026-08-24T10:10:00",
      next_business_action: "方向已批准；下一步进入产品研究并形成 Product Definition 草案。",
    });
    const load = vi.fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(radar([approvedDirection]));
    const review = vi.fn().mockResolvedValue({});

    render(<DemandRadarPage loadDemandRadar={load} reviewOpportunity={review} />);
    const button = await screen.findByRole("button", { name: "跟进这个方向" });
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(review).toHaveBeenCalledTimes(1));
    expect(review).toHaveBeenCalledWith("opp-1", { decision: "approve" });
    expect(await screen.findByText(/已决定跟进：七宗罪人格测试/)).toBeVisible();
    expect(screen.getByText("下一步：产品定义")).toBeVisible();
  });

  it("does not let the frontend approve a direction the backend marked unproven", async () => {
    render(<DemandRadarPage loadDemandRadar={vi.fn().mockResolvedValue(radar([
      direction({ can_follow_up: false }),
    ]))} />);

    expect(await screen.findByText("共同具体需求尚未被持久化证明，当前不能跟进。")).toBeVisible();
    expect(screen.queryByRole("button", { name: "跟进这个方向" })).not.toBeInTheDocument();
  });

  it("requires a reason before rejecting and blocks duplicate review writes", async () => {
    let resolve!: (value: unknown) => void;
    const review = vi.fn(() => new Promise(done => { resolve = done; }));
    const load = vi.fn().mockResolvedValue(radar());
    render(<DemandRadarPage loadDemandRadar={load} reviewOpportunity={review} />);

    await screen.findByRole("heading", { name: "七宗罪人格测试", level: 3 });
    fireEvent.click(screen.getByText("放弃这个方向"));
    const confirm = screen.getByRole("button", { name: "确认放弃" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("放弃 七宗罪人格测试 的原因"), { target: { value: "交付风险太高" } });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(review).toHaveBeenCalledTimes(1);
    expect(review).toHaveBeenCalledWith("opp-1", { decision: "reject", reason: "交付风险太高" });
    resolve({});
  });

  it("labels legacy products without pretending the new Product Definition gate passed", async () => {
    render(<DemandRadarPage loadDemandRadar={vi.fn().mockResolvedValue(radar([
      direction({
        review_status: "approved",
        journey_stage: "legacy_product_workspace",
        linked_products: [{ product_id: "legacy-1", name: "历史测试产品", target_user: "人格测试用户", created_at: "2026-08-20T10:00:00" }],
        next_business_action: "已有历史产品工作区；进入 Stage 7 产品定义前先核对现有产品资料。",
      }),
    ]))} />);

    expect(await screen.findByText("历史测试产品 · 目标用户：人格测试用户")).toBeVisible();
    expect(screen.getByText(/不等于新的 Product Definition Gate 已通过/)).toBeVisible();
  });

  it("keeps API failure visible and retryable", async () => {
    const load = vi.fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(radar([]));
    render(<DemandRadarPage loadDemandRadar={load} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("需求雷达暂时不可用");
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByText("目前没有可跟进方向")).toBeVisible();
    expect(load).toHaveBeenCalledTimes(2);
  });
});
