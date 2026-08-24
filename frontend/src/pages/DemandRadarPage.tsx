import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchDemandRadar,
  reviewOpportunity as postReviewOpportunity,
  type DemandRadar,
  type DemandRadarDirection,
  type Opportunity,
} from "../api/client";

type ReviewPayload = { decision: "approve" | "reject"; reason?: string };

export interface DemandRadarPageProps {
  loadDemandRadar?: () => Promise<DemandRadar>;
  reviewOpportunity?: (opportunityId: string, payload: ReviewPayload) => Promise<Opportunity | unknown>;
}

const evidenceLevelLabel = (level: DemandRadarDirection["evidence_level"]) => ({
  warming_candidate: "正在升温",
  validated_candidate: "已验证方向",
  legacy_ungraded: "历史候选",
}[level]);

const journeyLabel = (stage: DemandRadarDirection["journey_stage"]) => ({
  demand_decision: "待你决定",
  product_definition: "下一步：产品定义",
  legacy_product_workspace: "已有历史产品资料",
  closed: "已结束",
}[stage]);

function SummaryCard({ label, value, hint }: { label: string; value: number; hint: string }) {
  return <article className="demand-summary-card">
    <span>{label}</span>
    <strong>{value}</strong>
    <small>{hint}</small>
  </article>;
}

function ProductEvidence({ direction }: { direction: DemandRadarDirection }) {
  if (!direction.representative_products.length) {
    return <p className="demand-muted">这条历史方向没有可展示的代表商品摘要。</p>;
  }
  return <div className="representative-products">
    {direction.representative_products.map(product => <article className="representative-product" key={`${product.account_user_id}:${product.product_id}`}>
      <div className="representative-product__placeholder" aria-label={`${product.title ?? product.product_id} 的图片证据摘要`}>
        <strong>{product.image_evidence_count}</strong>
        <span>份图片证据</span>
      </div>
      <div>
        <h4>{product.title ?? "未记录商品标题"}</h4>
        <p className="product-signal">
          {product.price ? <span>价格 {product.price}</span> : <span>价格未记录</span>}
          {product.sold ? <span>销量 {product.sold}</span> : <span>销量未记录</span>}
        </p>
        <p className="demand-muted">账号 {product.account_user_id}</p>
        {product.source_url ? <a href={product.source_url} target="_blank" rel="noreferrer">查看原商品</a> : null}
      </div>
    </article>)}
  </div>;
}

function DirectionEvidence({ direction }: { direction: DemandRadarDirection }) {
  return <details className="demand-evidence-details">
    <summary>查看支撑证据</summary>
    <div className="demand-evidence-body">
      <p><strong>证据规模：</strong> {direction.supporting_account_count} 个账号 · {direction.supporting_product_count} 个商品 · {direction.supporting_note_count} 篇笔记 · {direction.image_evidence_count} 份商品图片证据</p>
      <p className="demand-evidence-note">当前页面只展示安全业务摘要和原商品链接，不暴露本机 Artifact 路径。教程要求的“工作台内直接逐张看商品图”需要单独的安全媒体读取接口，不能用本地路径硬接。</p>
    </div>
  </details>;
}

export function DemandRadarPage({
  loadDemandRadar = fetchDemandRadar,
  reviewOpportunity = postReviewOpportunity,
}: DemandRadarPageProps) {
  const [state, setState] = useState<
    { kind: "loading" } |
    { kind: "error" } |
    { kind: "ready"; data: DemandRadar }
  >({ kind: "loading" });
  const [rejectReasons, setRejectReasons] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const pendingRef = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      setState({ kind: "ready", data: await loadDemandRadar() });
    } catch {
      setState({ kind: "error" });
    }
  }, [loadDemandRadar]);

  useEffect(() => { void refresh(); }, [refresh]);

  const review = async (direction: DemandRadarDirection, payload: ReviewPayload) => {
    if (pendingRef.current !== null) return;
    pendingRef.current = direction.opportunity_id;
    setPendingId(direction.opportunity_id);
    setNotice(null);
    setActionError(null);
    try {
      await reviewOpportunity(direction.opportunity_id, payload);
      setNotice(payload.decision === "approve"
        ? `已决定跟进：${direction.title}。下一步将进入产品研究与 Product Definition。`
        : `已放弃：${direction.title}。`);
      setState({ kind: "ready", data: await loadDemandRadar() });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "操作失败，请稍后重试。");
    } finally {
      pendingRef.current = null;
      setPendingId(null);
    }
  };

  if (state.kind === "loading") {
    return <main className="workbench-page" id="main-content"><section className="loading-panel" aria-busy="true"><p role="status">正在整理今天的需求雷达</p></section></main>;
  }
  if (state.kind === "error") {
    return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>需求雷达暂时不可用</h1><p>业务投影没有返回有效结果，底层数据不会因此被修改。</p><button type="button" onClick={() => void refresh()}>重新加载</button></section></main>;
  }

  const { summary, directions } = state.data;
  const pendingDirections = directions.filter(item => item.review_status === "pending_review");
  const activeDirections = directions.filter(item => item.review_status !== "rejected");
  const closedDirections = directions.filter(item => item.review_status === "rejected");

  return <main className="workbench-page demand-radar-page" id="main-content">
    <header className="page-heading demand-heading">
      <div><p className="eyebrow">小红书虚拟产品 · 找需求</p><h1>需求雷达</h1><p>AI 把已经形成跨账号证据的方向摆出来。你只需要判断：跟进、继续观察，还是放弃。</p></div>
      <a className="secondary-link" href="/opportunities/analysis">高级：运行跨账号需求分析</a>
    </header>

    <section className="demand-overview" aria-label="需求雷达概览">
      <SummaryCard label="今日新增" value={summary.new_today_count} hint={`共 ${summary.total_direction_count} 个方向`} />
      <SummaryCard label="正在升温" value={summary.warming_count} hint={`${summary.validated_count} 个已验证`} />
      <SummaryCard label="待你决定" value={summary.pending_decision_count} hint="需要人工选择" />
      <SummaryCard label="已批准" value={summary.approved_count} hint={`${summary.linked_product_count} 个已有产品资料`} />
    </section>

    {notice ? <p className="action-notice" role="status">{notice}</p> : null}
    {actionError ? <p className="action-error" role="alert">{actionError}</p> : null}

    <div className="demand-layout">
      <section className="demand-main">
        <div className="panel-heading"><div><h2>值得关注的方向</h2><p>按“待决定优先、证据更强优先、更新更近优先”排列。</p></div></div>
        {activeDirections.length === 0 ? <section className="message-panel" role="status"><h3>目前没有可跟进方向</h3><p>这里不会生成演示机会；只有真实持久化的跨账号候选才会出现。</p></section> : <div className="demand-direction-grid">
          {activeDirections.map(direction => <article className="demand-direction-card" key={direction.opportunity_id}>
            <header>
              <div className="demand-badges">
                {direction.is_new_today ? <span className="business-badge">今日新增</span> : null}
                <span className={`business-badge business-badge--${direction.evidence_level}`}>{evidenceLevelLabel(direction.evidence_level)}</span>
                <span className={`business-badge business-badge--journey-${direction.journey_stage}`}>{journeyLabel(direction.journey_stage)}</span>
              </div>
              <h3>{direction.title}</h3>
              <p>{direction.summary}</p>
            </header>
            <div className="demand-metrics" aria-label={`${direction.title} 的证据摘要`}>
              <span><strong>{direction.supporting_account_count}</strong> 支撑账号</span>
              <span><strong>{direction.supporting_product_count}</strong> 商品</span>
              <span><strong>{direction.image_evidence_count}</strong> 图片证据</span>
            </div>
            <ProductEvidence direction={direction} />
            <div className="next-business-action"><span>下一步</span><strong>{direction.next_business_action}</strong></div>

            {direction.linked_products.length ? <div className="legacy-product-note"><strong>已有历史产品资料</strong>{direction.linked_products.map(product => <p key={product.product_id}>{product.name} · 目标用户：{product.target_user}</p>)}<small>这只证明旧产品工作区存在，不等于新的 Product Definition Gate 已通过。</small></div> : null}

            {direction.review_status === "pending_review" ? <div className="demand-decision-box">
              {direction.can_follow_up ? <button disabled={pendingId !== null} type="button" onClick={() => void review(direction, { decision: "approve" })}>跟进这个方向</button> : <p className="decision-warning">共同具体需求尚未被持久化证明，当前不能跟进。</p>}
              <p className="demand-muted">继续观察：不做任何状态变更，这个方向会继续留在“待你决定”。</p>
              <details>
                <summary>放弃这个方向</summary>
                <label>放弃原因<textarea aria-label={`放弃 ${direction.title} 的原因`} value={rejectReasons[direction.opportunity_id] ?? ""} onChange={event => setRejectReasons(current => ({ ...current, [direction.opportunity_id]: event.target.value }))} /></label>
                <button className="button-secondary" disabled={pendingId !== null || !(rejectReasons[direction.opportunity_id] ?? "").trim()} type="button" onClick={() => void review(direction, { decision: "reject", reason: rejectReasons[direction.opportunity_id].trim() })}>确认放弃</button>
              </details>
            </div> : null}
            <DirectionEvidence direction={direction} />
          </article>)}
        </div>}
      </section>

      <aside className="attention-rail" aria-label="今天需要关注">
        <h2>今天需要你关注</h2>
        <div className="attention-stat"><strong>{pendingDirections.length}</strong><span>个方向待决定</span></div>
        {pendingDirections.length ? <ul>{pendingDirections.slice(0, 6).map(item => <li key={item.opportunity_id}><strong>{item.title}</strong><span>{evidenceLevelLabel(item.evidence_level)} · {item.supporting_account_count} 个账号</span></li>)}</ul> : <p>当前没有等待你决定的方向。</p>}
        <hr />
        <p><strong>采集和技术状态</strong></p>
        <p className="demand-muted">榜单采集、手机任务和 Agent 审计仍保留在“采集控制 / Agent 工作台”，不挤进业务主界面。</p>
        <a href="/radar">打开采集控制</a>
      </aside>
    </div>

    {closedDirections.length ? <details className="closed-directions"><summary>已放弃方向（{closedDirections.length}）</summary><ul>{closedDirections.map(item => <li key={item.opportunity_id}>{item.title}</li>)}</ul></details> : null}
  </main>;
}
