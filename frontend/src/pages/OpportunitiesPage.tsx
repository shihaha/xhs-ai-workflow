import { useCallback, useEffect, useRef, useState } from "react";

import {
  createAnalysis as postAnalysis,
  fetchAccounts,
  fetchAnalyses,
  fetchAnalysisEvidence,
  fetchOpportunities,
  reviewOpportunity as postReview,
  type Account,
  type Analysis,
  type AnalysisOutput,
  type AnalysisEvidence,
  type Opportunity,
} from "../api/client";

type Data = { opportunities: Opportunity[]; accounts: Account[]; evidence: AnalysisEvidence[]; analyses?: Analysis[] };
type AnalysisPayload = { analysis_type: "account_opportunity"; account_user_ids: string[]; evidence_ids: string[] };
type ReviewPayload = { decision: "approve" | "reject"; reason?: string };
export interface OpportunitiesPageProps {
  loadOpportunities?: () => Promise<Data>;
  createAnalysis?: (payload: AnalysisPayload) => Promise<unknown>;
  reviewOpportunity?: (opportunityId: string, payload: ReviewPayload) => Promise<Opportunity>;
}

const reviewStatusLabel = (status: Opportunity["review_status"]) => ({
  pending_review: "等待审核",
  approved: "已批准",
  rejected: "已拒绝",
}[status]);

const evidenceLevelLabel = (level: Opportunity["evidence_level"]) => ({
  warming_candidate: "升温候选",
  validated_candidate: "已验证候选",
  legacy_ungraded: "历史未分级候选",
}[level]);

const defaultLoad = async (): Promise<Data> => {
  const [opportunities, accounts, evidence, analyses] = await Promise.all([
    fetchOpportunities(), fetchAccounts(), fetchAnalysisEvidence(), fetchAnalyses(),
  ]);
  return { opportunities, accounts, evidence, analyses };
};

function DemandAnalysisDetails({ output }: { output: AnalysisOutput }) {
  const profiles = output.account_demand_profiles ?? [];
  const conclusion = output.cross_account_conclusion;
  if (!conclusion) return <section className="message-panel" role="note"><h3>共同需求判断说明缺失</h3><p>这条历史分析没有记录共同需求、账号差异和形成理由。在说明补齐前，不应据此批准候选。</p></section>;
  return <section className="operator-panel">
    <h3>{conclusion.has_specific_shared_demand ? "AI判断：存在共同具体需求" : "AI判断：不存在共同具体需求"}</h3>
    <p><strong>AI认为的共同需求：</strong> {conclusion.common_demand ?? "没有可信的共同具体需求"}</p>
    <h4>两个账号各自在证明什么</h4>
    <ul>{profiles.map(profile => <li key={profile.account_user_id}><strong>{profile.account_user_id}</strong><br />主要售卖内容：{profile.primary_offering}<br />目标用户：{profile.target_user}<br />核心购买动机：{profile.core_purchase_motivation}<br />产品或交付形态：{profile.delivery_format}<br />使用场景：{profile.usage_scenarios.join("；")}<br />证据：{profile.evidence_ids.join(", ")}</li>)}</ul>
    <h4>它们的共同点</h4><ul>{conclusion.commonalities.length ? conclusion.commonalities.map(item => <li key={item}>{item}</li>) : <li>没有足以形成机会的具体共同点</li>}</ul>
    <h4>关键差异</h4><ul>{conclusion.key_differences.length ? conclusion.key_differences.map(item => <li key={item}>{item}</li>) : <li>没有记录关键差异</li>}</ul>
    <h4>为什么形成或不形成候选</h4><p>{conclusion.rationale}</p>
    <p><strong>判断所引用的证据：</strong> {conclusion.evidence_ids.join(", ")}</p>
  </section>;
}

export function OpportunitiesPage({ loadOpportunities = defaultLoad, createAnalysis = postAnalysis, reviewOpportunity = postReview }: OpportunitiesPageProps) {
  const [state, setState] = useState<{ kind: "loading" } | { kind: "error" } | { kind: "ready"; data: Data }>({ kind: "loading" });
  const [selectedAccounts, setSelectedAccounts] = useState<string[]>([]);
  const [rejectionReasons, setRejectionReasons] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const refresh = useCallback(async () => {
    setState({ kind: "loading" });
    try { setState({ kind: "ready", data: await loadOpportunities() }); }
    catch { setState({ kind: "error" }); }
  }, [loadOpportunities]);
  useEffect(() => { void refresh(); }, [refresh]);
  const act = async (action: () => Promise<string>) => {
    if (pendingRef.current) return;
    pendingRef.current = true; setPending(true); setActionError(null);
    try { setNotice(await action()); setState({ kind: "ready", data: await loadOpportunities() }); }
    catch (error) { setActionError(error instanceof Error ? error.message : "操作失败，请稍后重试。"); }
    finally { pendingRef.current = false; setPending(false); }
  };

  if (state.kind === "loading") return <main className="workbench-page" id="main-content"><section className="loading-panel" aria-busy="true"><p role="status">正在加载机会候选</p></section></main>;
  if (state.kind === "error") return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>无法加载机会候选</h1><p>机会证据接口没有返回有效结果。</p><button type="button" onClick={() => void refresh()}>重新加载</button></section></main>;

  const { opportunities, accounts, evidence, analyses = [] } = state.data;
  const crossAccountAnalyses = analyses.filter(item => item.analysis_type === "account_opportunity" && item.status === "succeeded" && item.output);
  const analysisById = new Map(crossAccountAnalyses.map(item => [item.id, item]));
  const completeness = new Map(accounts.map(account => {
    const rows = evidence.filter(item => item.account_user_id === account.user_id && item.eligible_for_opportunity);
    return [account.user_id, { shop: rows.filter(item => item.kind === "shop_collection_result"), notes: rows.filter(item => item.kind === "account_note") }] as const;
  }));
  const selectedEvidence = selectedAccounts.flatMap(accountId => {
    const facts = completeness.get(accountId);
    return [...(facts?.shop ?? []), ...(facts?.notes ?? [])].map(item => item.evidence_id);
  });
  const canApprove = (opportunity: Opportunity) =>
    analysisById.get(opportunity.analysis_id)?.output?.cross_account_conclusion
      ?.has_specific_shared_demand === true;

  return <main className="workbench-page" id="main-content">
    <header className="page-heading"><p className="eyebrow">跨账号需求验证</p><h1>机会审核</h1><p>单个账号只能形成观察信号。候选等级和证据归属均由持久化证据计算。</p></header>
    <section className="message-panel" role="note"><h2>Phase A 范围边界</h2><p>这里不能创建产品。Phase B 尚未开始，目前只允许人工审核跨账号候选。</p></section>
    {notice ? <p className="action-notice" role="status">{notice}</p> : null}{actionError ? <p className="action-error" role="alert">{actionError}</p> : null}
    <section className="operator-panel"><div className="panel-heading"><h2>创建跨账号需求聚类</h2><p>已选择 {selectedAccounts.length} 个账号</p></div>
      {accounts.length === 0 ? <p className="panel-empty">目前没有可用的榜单账号。</p> : <form className="action-form" onSubmit={event => { event.preventDefault(); void act(async () => { if (selectedAccounts.length < 2) throw new Error("请至少选择两个证据完整的账号。"); await createAnalysis({ analysis_type: "account_opportunity", account_user_ids: selectedAccounts, evidence_ids: selectedEvidence }); return "跨账号分析已完成。请在下方查看已保存的候选及其审核状态。"; }); }}>
        <fieldset><legend>具备完整可信证据的账号</legend>{accounts.map(account => { const facts = completeness.get(account.user_id); const complete = Boolean(facts?.shop.length && facts?.notes.length); return <label className="check-label" key={account.user_id}><input aria-label={`选择 ${account.account_name}`} type="checkbox" disabled={!complete || pending} checked={selectedAccounts.includes(account.user_id)} onChange={event => setSelectedAccounts(current => event.target.checked ? [...current, account.user_id] : current.filter(id => id !== account.user_id))} />{account.account_name} · 店铺证据 {facts?.shop.length ?? 0} · 笔记证据 {facts?.notes.length ?? 0} · {complete ? "完整" : "不完整"}</label>; })}</fieldset>
        <p className="field-help">两个账号形成“升温候选”，三个及以上账号形成“已验证候选”。提交时会包含每个账号全部合格的店铺和笔记证据。</p><button disabled={pending || selectedAccounts.length < 2} type="submit">运行跨账号需求分析</button>
      </form>}
    </section>
    {crossAccountAnalyses.length ? <section className="operator-panel"><div className="panel-heading"><h2>AI 判断过程</h2><p>以下内容来自已经持久化的模型结构化结果。</p></div>{crossAccountAnalyses.map(analysis => <article key={analysis.id}><p><strong>分析记录：</strong> {analysis.id} · {analysis.provider ?? "provider 未记录"} / {analysis.model ?? "model 未记录"} · {analysis.prompt_version ?? "prompt 未记录"}</p><DemandAnalysisDetails output={analysis.output!} /></article>)}</section> : null}
    {opportunities.length === 0 ? <section className="message-panel" role="status"><h2>暂时没有跨账号候选</h2><p>只有至少两个证据完整的账号通过服务端验证，分析结果才会保存为候选。</p></section> : <ol className="opportunity-list">{opportunities.map(opportunity => <li className="opportunity-record" key={opportunity.id}>
      <header><span className={`state state--${opportunity.review_status}`}>{reviewStatusLabel(opportunity.review_status)}</span><h2>{opportunity.title}</h2></header><p>{opportunity.summary}</p><p><strong>证据等级：</strong> {evidenceLevelLabel(opportunity.evidence_level)} · {opportunity.supporting_account_count} 个支撑账号</p>
      <DemandAnalysisDetails output={analysisById.get(opportunity.analysis_id)?.output ?? {}} />
      <h3>支撑账号</h3><ul>{opportunity.supporting_accounts.map(item => <li key={item.account_user_id}><strong>{item.account_user_id}</strong> · 店铺证据 {item.shop_evidence_ids.join(", ")} · 笔记证据 {item.note_evidence_ids.join(", ")}</li>)}</ul>
      <h3>支撑商品和图片</h3><ul>{opportunity.supporting_products.map(item => <li key={`${item.account_user_id}:${item.product_id}`}><a href={item.source_url} target="_blank" rel="noreferrer">{item.title ?? item.product_id}</a> · {item.account_user_id} · {item.image_evidence_count} 份图片证据</li>)}</ul>
      <h3>支撑笔记</h3><ul>{opportunity.supporting_notes.map(item => <li key={`${item.account_user_id}:${item.note_id}`}><a href={item.source_url} target="_blank" rel="noreferrer">{item.title ?? item.note_id}</a> · {item.account_user_id} · <code>{item.evidence_id}</code></li>)}</ul>
      {opportunity.review_status === "pending_review" ? <div className="action-form"><label>拒绝原因<textarea aria-label={`拒绝 ${opportunity.title} 的原因`} value={rejectionReasons[opportunity.id] ?? ""} onChange={event => setRejectionReasons(current => ({ ...current, [opportunity.id]: event.target.value }))} /></label><div className="button-row">{canApprove(opportunity) ? <button disabled={pending} type="button" onClick={() => void act(async () => { await reviewOpportunity(opportunity.id, { decision: "approve" }); return `已批准：${opportunity.title}`; })}>批准：{opportunity.title}</button> : null}<button disabled={pending || !(rejectionReasons[opportunity.id] ?? "").trim()} type="button" onClick={() => void act(async () => { await reviewOpportunity(opportunity.id, { decision: "reject", reason: rejectionReasons[opportunity.id].trim() }); return `已拒绝：${opportunity.title}`; })}>拒绝：{opportunity.title}</button></div></div> : opportunity.review_status === "rejected" ? <p><strong>拒绝原因：</strong> {opportunity.rejection_reason}</p> : <p>人工批准时间：{opportunity.reviewed_at ?? "时间不可用"}。</p>}
    </li>)}</ol>}
  </main>;
}
