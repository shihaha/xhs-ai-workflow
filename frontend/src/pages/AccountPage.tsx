import { useCallback, useEffect, useMemo, useState } from "react";

import { createAnalysis as postAnalysis, fetchAccounts, fetchAnalyses, fetchAnalysisEvidence, fetchDevices, fetchJobs, queueShopCollection, type Account, type Analysis, type AnalysisEvidence, type DeviceHealth, type Job } from "../api/client";

type AccountData = { account: Account | null; evidence: AnalysisEvidence[]; analyses: Analysis[]; jobs: Job[]; devices: DeviceHealth[] };
type AnalysisPayload = { analysis_type: "account_report"; account_user_id: string; account_user_ids: string[]; evidence_ids: string[] } | { analysis_type: "account_opportunity"; account_user_ids: string[]; evidence_ids: string[] };
export interface AccountPageProps { accountId: string; loadAccount?: () => Promise<AccountData>; queueShop?: (payload: Record<string, unknown>) => Promise<{ job_id: string; status: "queued" }>; createAnalysis?: (payload: AnalysisPayload) => Promise<unknown>; }

const loadFor = async (accountId: string): Promise<AccountData> => {
  const [accounts, evidence, analyses, jobs, devices] = await Promise.all([fetchAccounts(), fetchAnalysisEvidence(accountId), fetchAnalyses(), fetchJobs(), fetchDevices()]);
  return { account: accounts.find(item => item.user_id === accountId) ?? null, evidence, analyses: analyses.filter(item => item.account_user_id === accountId || item.account_user_ids?.includes(accountId)), jobs: jobs.filter(job => job.input.account_user_id === accountId), devices };
};

export function AccountPage({ accountId, loadAccount, queueShop = queueShopCollection, createAnalysis = postAnalysis }: AccountPageProps) {
  const loader = useMemo(() => loadAccount ?? (() => loadFor(accountId)), [accountId, loadAccount]);
  const [state, setState] = useState<{ kind: "loading" } | { kind: "error" } | { kind: "ready"; data: AccountData }>({ kind: "loading" });
  const [expected, setExpected] = useState("0");
  const [verificationDir, setVerificationDir] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const refresh = useCallback(async () => { setState({ kind: "loading" }); try { setState({ kind: "ready", data: await loader() }); } catch { setState({ kind: "error" }); } }, [loader]);
  const reloadFacts = useCallback(async () => { setState({ kind: "ready", data: await loader() }); }, [loader]);
  useEffect(() => { void refresh(); }, [refresh]);

  if (state.kind === "loading") return <main className="workbench-page" id="main-content"><section className="loading-panel" aria-busy="true"><p role="status">Loading account evidence</p></section></main>;
  if (state.kind === "error") return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>Could not load account evidence</h1><button onClick={() => void refresh()} type="button">Retry account</button></section></main>;
  if (!state.data.account) return <main className="workbench-page" id="main-content"><section className="message-panel" role="status"><h1>Account not found in persisted ranking evidence</h1><p>No matching account was returned for <code>{accountId}</code>.</p></section></main>;
  const { account, evidence, analyses, jobs, devices } = state.data;
  const device = devices[0];
  const act = async (action: () => Promise<string>) => { setActionError(null); try { setNotice(await action()); try { await reloadFacts(); } catch { setActionError("Action completed, but persisted facts could not be reloaded."); } } catch (error) { setActionError(error instanceof Error ? error.message : "Action failed."); } };
  return <main className="workbench-page" id="main-content">
    <header className="page-heading"><p className="eyebrow">Account investigation</p><h1>{account.account_name}</h1><p><code>{account.user_id}</code> · score {account.score} · {account.fans} fans</p></header>
    {device ? <p className={`inline-status state--${device.status}`}>Device {device.status}: {device.detail}</p> : <p className="inline-status state--unavailable">No device health fact returned.</p>}
    {notice ? <p className="action-notice" role="status">{notice}</p> : null}{actionError ? <p className="action-error" role="alert">{actionError}</p> : null}
    <div className="workflow-grid">
      <section className="operator-panel"><div className="panel-heading"><h2>True-device shop verification</h2></div><form className="action-form" onSubmit={event => { event.preventDefault(); void act(async () => { const result = await queueShop({ account_user_id: account.user_id, account_name: account.account_name, expected_count: Number(expected), ...(device?.device_id ? { device_id: device.device_id } : {}), ...(verificationDir.trim() ? { verification_dir: verificationDir.trim() } : {}) }); return `Queued job ${result.job_id}`; }); }}><label>Expected shop products<input aria-label="Expected shop products" min="0" required type="number" value={expected} onChange={event => setExpected(event.target.value)} /></label><label>Verification evidence directory<input aria-label="Verification evidence directory" placeholder="evidence/shops/account-id" value={verificationDir} onChange={event => setVerificationDir(event.target.value)} /></label><p className="field-help">Use a runtime-relative directory containing collection.json and exact N/N product evidence. Leave empty to queue collection and receive a factual verification-pending state.</p><button type="submit">Queue device collection</button></form></section>
      <section className="operator-panel"><div className="panel-heading"><h2>Evidence-bound analysis</h2></div>{evidence.length === 0 ? <p className="panel-empty">No eligible evidence returned.</p> : <form className="action-form" onSubmit={event => { event.preventDefault(); void act(async () => { await createAnalysis({ analysis_type: "account_report", account_user_id: account.user_id, account_user_ids: [], evidence_ids: selected }); return "Account analysis request completed. Refresh to inspect the persisted result."; }); }}><fieldset><legend>Persisted evidence</legend>{evidence.map(item => <label className="check-label" key={item.evidence_id}><input aria-label={item.evidence_id} checked={selected.includes(item.evidence_id)} type="checkbox" onChange={event => setSelected(current => event.target.checked ? [...current, item.evidence_id] : current.filter(id => id !== item.evidence_id))} />{item.evidence_id} · {item.kind}{item.eligible_for_opportunity ? " · deep-verified" : ""}</label>)}</fieldset><div className="button-row"><button disabled={selected.length === 0} type="submit">Generate account report</button><button disabled={selected.length === 0} type="button" onClick={() => void act(async () => { await createAnalysis({ analysis_type: "account_opportunity", account_user_ids: [account.user_id], evidence_ids: selected }); return "Opportunity analysis request completed. Open Opportunities to inspect persisted cards."; })}>Generate opportunity analysis</button></div></form>}</section>
    </div>
    <section className="operator-panel"><div className="panel-heading"><h2>Collection jobs</h2><p>{jobs.length} returned</p></div>{jobs.length === 0 ? <p className="panel-empty">No device collection has been recorded for this account.</p> : <ol className="collection-list">{jobs.map(job => { const missing = shopMissingItems(job); return <li key={job.id}><header><strong>{job.state === "needs_human" ? "Human attention required" : job.state}</strong><span>{job.progress_current} / {job.progress_total ?? "unknown"} verified or collected</span></header><p>Stage: {job.current_stage ?? "not reported"}{job.error_category ? ` · Error: ${job.error_category}` : ""}</p>{missing.length ? <><h3>Reported missing items</h3><ul>{missing.map((item, index) => <li key={`${item.reference}-${index}`}>{item.reference} · {item.reason}</li>)}</ul></> : null}<a href={`/jobs#job-${encodeURIComponent(job.id)}-evidence`}>Inspect {job.artifacts.length} evidence {job.artifacts.length === 1 ? "item" : "items"}</a>{job.state === "needs_human" || job.state === "failed" ? <p className="field-help">Resolve the device, login, or evidence issue, then queue a new collection above. The prior job remains unchanged for audit.</p> : null}</li>; })}</ol>}</section>
    <section className="operator-panel"><div className="panel-heading"><h2>Analysis history</h2></div>{analyses.length === 0 ? <p className="panel-empty">No account analysis persisted.</p> : <ol className="analysis-list">{analyses.map(item => { const claims = analysisClaims(item.output); return <li key={item.id}><header><strong>{item.analysis_type}</strong><span className={`state state--${item.status}`}>{item.status}{item.error_category ? ` · ${item.error_category}` : ""}</span></header><p className="evidence-line">Evidence: {item.evidence_ids.join(", ") || "none reported"}</p>{claims.length ? <ul>{claims.map((claim, index) => <li key={`${item.id}-claim-${index}`}>{claim}</li>)}</ul> : null}{item.error_detail ? <p>{item.error_detail}</p> : null}{item.status !== "succeeded" ? <p className="field-help">Resolve the reported evidence or provider issue, then submit a new evidence-bound analysis above.</p> : null}</li>; })}</ol>}</section>
  </main>;
}

function analysisClaims(output: Record<string, unknown> | null | undefined): string[] {
  if (!output || !Array.isArray(output.claims)) return [];
  return output.claims.flatMap(value => value && typeof value === "object" && typeof (value as { claim?: unknown }).claim === "string" ? [(value as { claim: string }).claim] : []);
}

function shopMissingItems(job: Job): Array<{ reference: string; reason: string }> {
  return job.artifacts.flatMap(artifact => {
    const result = artifact.metadata.result;
    if (!result || typeof result !== "object" || !Array.isArray((result as { missing_items?: unknown }).missing_items)) return [];
    return (result as { missing_items: unknown[] }).missing_items.flatMap(value => value && typeof value === "object" && typeof (value as { reference?: unknown }).reference === "string" && typeof (value as { reason?: unknown }).reason === "string" ? [{ reference: (value as { reference: string }).reference, reason: (value as { reason: string }).reason }] : []);
  });
}
