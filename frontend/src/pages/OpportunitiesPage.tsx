import { useCallback, useEffect, useRef, useState } from "react";

import {
  createAnalysis as postAnalysis,
  fetchAccounts,
  fetchAnalysisEvidence,
  fetchOpportunities,
  reviewOpportunity as postReview,
  type Account,
  type AnalysisEvidence,
  type Opportunity,
} from "../api/client";

type Data = { opportunities: Opportunity[]; accounts: Account[]; evidence: AnalysisEvidence[] };
type AnalysisPayload = { analysis_type: "account_opportunity"; account_user_ids: string[]; evidence_ids: string[] };
type ReviewPayload = { decision: "approve" | "reject"; reason?: string };
export interface OpportunitiesPageProps {
  loadOpportunities?: () => Promise<Data>;
  createAnalysis?: (payload: AnalysisPayload) => Promise<unknown>;
  reviewOpportunity?: (opportunityId: string, payload: ReviewPayload) => Promise<Opportunity>;
}

const defaultLoad = async (): Promise<Data> => {
  const [opportunities, accounts, evidence] = await Promise.all([
    fetchOpportunities(), fetchAccounts(), fetchAnalysisEvidence(),
  ]);
  return { opportunities, accounts, evidence };
};

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
    catch (error) { setActionError(error instanceof Error ? error.message : "Action failed."); }
    finally { pendingRef.current = false; setPending(false); }
  };

  if (state.kind === "loading") return <main className="workbench-page" id="main-content"><section className="loading-panel" aria-busy="true"><p role="status">Loading opportunities</p></section></main>;
  if (state.kind === "error") return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>Could not load opportunities</h1><p>The opportunity evidence API did not return a result.</p><button type="button" onClick={() => void refresh()}>Retry opportunities</button></section></main>;

  const { opportunities, accounts, evidence } = state.data;
  const completeness = new Map(accounts.map(account => {
    const rows = evidence.filter(item => item.account_user_id === account.user_id && item.eligible_for_opportunity);
    return [account.user_id, { shop: rows.filter(item => item.kind === "shop_collection_result"), notes: rows.filter(item => item.kind === "account_note") }] as const;
  }));
  const selectedEvidence = selectedAccounts.flatMap(accountId => {
    const facts = completeness.get(accountId);
    return [...(facts?.shop ?? []), ...(facts?.notes ?? [])].map(item => item.evidence_id);
  });

  return <main className="workbench-page" id="main-content">
    <header className="page-heading"><p className="eyebrow">Cross-account demand validation</p><h1>Opportunities</h1><p>Single accounts are observations. Candidate levels and ownership are computed from persisted evidence.</p></header>
    <section className="message-panel" role="note"><h2>Phase A boundary</h2><p>Product building is not available here. Phase B has not started; only human review of cross-account candidates is allowed.</p></section>
    {notice ? <p className="action-notice" role="status">{notice}</p> : null}{actionError ? <p className="action-error" role="alert">{actionError}</p> : null}
    <section className="operator-panel"><div className="panel-heading"><h2>Create a cross-account cluster</h2><p>{selectedAccounts.length} accounts selected</p></div>
      {accounts.length === 0 ? <p className="panel-empty">No ranked accounts are available.</p> : <form className="action-form" onSubmit={event => { event.preventDefault(); void act(async () => { if (selectedAccounts.length < 2) throw new Error("Select at least two complete accounts."); await createAnalysis({ analysis_type: "account_opportunity", account_user_ids: selectedAccounts, evidence_ids: selectedEvidence }); return "Cross-account analysis completed. Inspect the persisted candidate and review status below."; }); }}>
        <fieldset><legend>Accounts with complete trusted evidence</legend>{accounts.map(account => { const facts = completeness.get(account.user_id); const complete = Boolean(facts?.shop.length && facts?.notes.length); return <label className="check-label" key={account.user_id}><input aria-label={`Select ${account.account_name}`} type="checkbox" disabled={!complete || pending} checked={selectedAccounts.includes(account.user_id)} onChange={event => setSelectedAccounts(current => event.target.checked ? [...current, account.user_id] : current.filter(id => id !== account.user_id))} />{account.account_name} · shop {facts?.shop.length ?? 0} · notes {facts?.notes.length ?? 0} · {complete ? "complete" : "incomplete"}</label>; })}</fieldset>
        <p className="field-help">Two accounts become warming_candidate; three or more become validated_candidate. All eligible shop and account-note IDs for each selection are submitted.</p><button disabled={pending || selectedAccounts.length < 2} type="submit">Run cross-account clustering</button>
      </form>}
    </section>
    {opportunities.length === 0 ? <section className="message-panel" role="status"><h2>No cross-account candidates yet</h2><p>A result is not persisted as a candidate unless at least two complete account evidence sets pass server verification.</p></section> : <ol className="opportunity-list">{opportunities.map(opportunity => <li className="opportunity-record" key={opportunity.id}>
      <header><span className={`state state--${opportunity.review_status}`}>{opportunity.review_status}</span><h2>{opportunity.title}</h2></header><p>{opportunity.summary}</p><p><strong>Evidence level:</strong> {opportunity.evidence_level} · {opportunity.supporting_account_count} supporting accounts</p>
      <h3>Supporting accounts</h3><ul>{opportunity.supporting_accounts.map(item => <li key={item.account_user_id}><strong>{item.account_user_id}</strong> · shop {item.shop_evidence_ids.join(", ")} · notes {item.note_evidence_ids.join(", ")}</li>)}</ul>
      <h3>Supporting products and images</h3><ul>{opportunity.supporting_products.map(item => <li key={`${item.account_user_id}:${item.product_id}`}><a href={item.source_url} target="_blank" rel="noreferrer">{item.title ?? item.product_id}</a> · {item.account_user_id} · {item.image_evidence_count} image evidence</li>)}</ul>
      <h3>Supporting notes</h3><ul>{opportunity.supporting_notes.map(item => <li key={`${item.account_user_id}:${item.note_id}`}><a href={item.source_url} target="_blank" rel="noreferrer">{item.title ?? item.note_id}</a> · {item.account_user_id} · <code>{item.evidence_id}</code></li>)}</ul>
      {opportunity.review_status === "pending_review" ? <div className="action-form"><label>Rejection reason<textarea aria-label={`Rejection reason for ${opportunity.title}`} value={rejectionReasons[opportunity.id] ?? ""} onChange={event => setRejectionReasons(current => ({ ...current, [opportunity.id]: event.target.value }))} /></label><div className="button-row"><button disabled={pending} type="button" onClick={() => void act(async () => { await reviewOpportunity(opportunity.id, { decision: "approve" }); return `Approved ${opportunity.title}`; })}>Approve {opportunity.title}</button><button disabled={pending || !(rejectionReasons[opportunity.id] ?? "").trim()} type="button" onClick={() => void act(async () => { await reviewOpportunity(opportunity.id, { decision: "reject", reason: rejectionReasons[opportunity.id].trim() }); return `Rejected ${opportunity.title}`; })}>Reject {opportunity.title}</button></div></div> : opportunity.review_status === "rejected" ? <p><strong>Rejection reason:</strong> {opportunity.rejection_reason}</p> : <p>Human approval recorded at {opportunity.reviewed_at ?? "time unavailable"}.</p>}
    </li>)}</ol>}
  </main>;
}
