import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  createAnalysis as postAnalysis,
  fetchAccountNotes,
  fetchAccountProfile,
  fetchAccounts,
  fetchAnalyses,
  fetchAnalysisEvidence,
  fetchDevices,
  fetchJob,
  fetchJobs,
  queueShopCollection,
  startAccountCollection as postAccountCollection,
  type Account,
  type AccountNote,
  type AccountProfile,
  type Analysis,
  type AnalysisEvidence,
  type CollectionQueued,
  type DeviceHealth,
  type Job,
} from "../api/client";

type AccountData = {
  account: Account | null;
  profile?: AccountProfile | null;
  notes?: AccountNote[];
  evidence: AnalysisEvidence[];
  analyses: Analysis[];
  jobs: Job[];
  devices: DeviceHealth[];
};
type AnalysisPayload =
  { analysis_type: "account_report"; account_user_id: string; account_user_ids: string[]; evidence_ids: string[] };
type TrackedCollection = { job_id: string; status: Job["state"]; polls: number; job?: Job; staleError?: string };

export interface AccountPageProps {
  accountId: string;
  loadAccount?: () => Promise<AccountData>;
  queueShop?: (payload: Record<string, unknown>) => Promise<{ job_id: string; status: "queued" }>;
  createAnalysis?: (payload: AnalysisPayload) => Promise<unknown>;
  startAccountCollection?: (userId: string, payload: { expected_note_count: number }) => Promise<CollectionQueued>;
  loadCollectionJob?: (jobId: string) => Promise<Job>;
  pollIntervalMs?: number;
  maxPolls?: number;
}

const terminal = new Set<Job["state"]>(["needs_human", "succeeded", "failed", "cancelled"]);

async function optionalFact<T>(load: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await load();
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return fallback;
    throw error;
  }
}

const loadFor = async (accountId: string): Promise<AccountData> => {
  const [accounts, evidence, analyses, jobs, devices] = await Promise.all([
    fetchAccounts(),
    fetchAnalysisEvidence(accountId),
    fetchAnalyses(),
    fetchJobs(),
    fetchDevices(),
  ]);
  const accountJobs = jobs.filter(job => job.input.account_user_id === accountId || job.input.user_id === accountId);
  const hasSuccessfulAccountCollection = accountJobs.some(job => job.type === "xhs_account_collection" && job.state === "succeeded");
  const [profile, notes] = hasSuccessfulAccountCollection
    ? await Promise.all([
        optionalFact(() => fetchAccountProfile(accountId), null),
        optionalFact(() => fetchAccountNotes(accountId), []),
      ])
    : [null, []];
  return {
    account: accounts.find(item => item.user_id === accountId) ?? null,
    profile,
    notes,
    evidence,
    analyses: analyses.filter(item => item.account_user_id === accountId || item.account_user_ids?.includes(accountId)),
    jobs: accountJobs,
    devices,
  };
};

export function AccountPage({
  accountId,
  loadAccount,
  queueShop = queueShopCollection,
  createAnalysis = postAnalysis,
  startAccountCollection = postAccountCollection,
  loadCollectionJob = fetchJob,
  pollIntervalMs = 1000,
  maxPolls = 60,
}: AccountPageProps) {
  const loader = useMemo(() => loadAccount ?? (() => loadFor(accountId)), [accountId, loadAccount]);
  const [state, setState] = useState<{ kind: "loading" } | { kind: "error" } | { kind: "ready"; data: AccountData }>({ kind: "loading" });
  const [expected, setExpected] = useState("0");
  const [expectedNotes, setExpectedNotes] = useState("1");
  const [verificationDir, setVerificationDir] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [trackedCollections, setTrackedCollections] = useState<TrackedCollection[]>([]);
  const [activeCollectionId, setActiveCollectionId] = useState<string | null>(null);
  const pendingRef = useRef(false);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    setState({ kind: "loading" });
    try { setState({ kind: "ready", data: await loader() }); }
    catch { setState({ kind: "error" }); }
  }, [loader]);
  const reloadFacts = useCallback(async () => {
    const data = await loader();
    if (mounted.current) setState({ kind: "ready", data });
  }, [loader]);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => { mounted.current = false; };
  }, [refresh]);

  const activeCollection = trackedCollections.find(item => item.job_id === activeCollectionId);
  useEffect(() => {
    if (!activeCollectionId || !activeCollection || terminal.has(activeCollection.status)) return;
    if (activeCollection.polls >= maxPolls) {
      setActiveCollectionId(current => current === activeCollectionId ? null : current);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void loadCollectionJob(activeCollectionId).then(async job => {
        if (cancelled || !mounted.current) return;
        setTrackedCollections(current => current.map(item => item.job_id === activeCollectionId
          ? { ...item, status: job.state, job, polls: item.polls + 1, staleError: undefined }
          : item));
        if (terminal.has(job.state)) {
          setActiveCollectionId(null);
          if (job.state === "succeeded") {
            try { await reloadFacts(); }
            catch { if (!cancelled && mounted.current) setActionError("Collection succeeded, but persisted account facts could not be reloaded."); }
          }
        }
      }).catch(error => {
        if (cancelled || !mounted.current) return;
        const message = error instanceof Error ? error.message : "Account collection job read failed.";
        setTrackedCollections(current => current.map(item => item.job_id === activeCollectionId
          ? { ...item, polls: item.polls + 1, staleError: message }
          : item));
      });
    }, pollIntervalMs);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [activeCollection, activeCollectionId, loadCollectionJob, maxPolls, pollIntervalMs, reloadFacts]);

  const act = async (action: () => Promise<string>) => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setPending(true);
    setActionError(null);
    try {
      const message = await action();
      if (mounted.current) setNotice(message);
    } catch (error) {
      if (mounted.current) setActionError(error instanceof Error ? error.message : "Action failed.");
    } finally {
      pendingRef.current = false;
      if (mounted.current) setPending(false);
    }
  };

  if (state.kind === "loading") return <main className="workbench-page" id="main-content"><section className="loading-panel" aria-busy="true"><p role="status">Loading account evidence</p></section></main>;
  if (state.kind === "error") return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>Could not load account evidence</h1><button onClick={() => void refresh()} type="button">Retry account</button></section></main>;
  if (!state.data.account) return <main className="workbench-page" id="main-content"><section className="message-panel" role="status"><h1>Account not found in persisted ranking evidence</h1><p>No matching account was returned for <code>{accountId}</code>.</p></section></main>;

  const { account, evidence, analyses, jobs, devices } = state.data;
  const profile = state.data.profile ?? null;
  const notes = state.data.notes ?? [];
  const device = devices[0];
  const persistedXhsJobs = jobs.filter(job => job.type === "xhs_account_collection");
  const deviceJobs = jobs.filter(job => job.type !== "xhs_account_collection");
  const accountNoteEvidence = evidence.filter(item => item.kind === "account_note");
  const trustedAccountNoteEvidence = accountNoteEvidence.filter(item => item.eligible_for_opportunity);
  const untrustedAccountNoteEvidence = accountNoteEvidence.filter(item => !item.eligible_for_opportunity);
  const selectableEvidenceIds = new Set(evidence.filter(item => item.kind !== "account_note" || item.eligible_for_opportunity).map(item => item.evidence_id));
  const selectedEvidenceIds = selected.filter(id => selectableEvidenceIds.has(id));

  return <main className="workbench-page" id="main-content">
    <header className="page-heading"><p className="eyebrow">Account investigation</p><h1>{account.account_name}</h1><p><code>{account.user_id}</code> · score {account.score} · {account.fans} fans</p></header>
    {device ? <p className={`inline-status state--${device.status}`}>Device {device.status}: {device.detail}</p> : <p className="inline-status state--unavailable">No device health fact returned.</p>}
    {notice ? <p className="action-notice" role="status">{notice}</p> : null}{actionError ? <p className="action-error" role="alert">{actionError}</p> : null}

    <section className="operator-panel">
      <div className="panel-heading"><h2>Read-only account and note collection</h2><p>Local xhs-cli · profile + exact note N/N</p></div>
      <form className="action-form" onSubmit={event => { event.preventDefault(); void act(async () => {
        const count = Number(expectedNotes);
        if (!Number.isInteger(count) || count < 0 || count > 1000) throw new Error("Expected notes must be a whole number from 0 to 1000.");
        const queued = await startAccountCollection(account.user_id, { expected_note_count: count });
        if (!mounted.current) return "";
        setTrackedCollections(current => [...current, { job_id: queued.job_id, status: queued.status, polls: 0 }]);
        setActiveCollectionId(queued.job_id);
        return `Account collection ${queued.job_id} queued.`;
      }); }}>
        <label>Expected account notes<input aria-label="Expected account notes" min="0" max="1000" step="1" required type="number" value={expectedNotes} onChange={event => setExpectedNotes(event.target.value)} /></label>
        <p className="field-help">Operator prerequisite: authenticate the trusted local xhs-cli session outside this application. This form never accepts Cookie, token, password, executable path, URL, or login actions.</p>
        <button disabled={pending || activeCollectionId !== null} type="submit">Collect account and notes</button>
      </form>
      {trackedCollections.length === 0 && persistedXhsJobs.length === 0 ? <p className="panel-empty">No account-note collection has been recorded in this page session.</p> : <ol className="collection-list">
        {trackedCollections.map(item => <li key={item.job_id}><header><strong>{item.status === "needs_human" ? "Human attention required" : item.status}</strong><code>{item.job_id}</code></header><p>{item.job ? `${item.job.progress_current} / ${item.job.progress_total ?? "unknown"} notes · ${item.job.current_stage ?? "stage not reported"}` : "Waiting for the first persisted job read."}{item.job?.error_category ? ` · ${item.job.error_category}` : ""}</p>{item.staleError ? <p className="action-error" role="alert">Account job read failed: {item.staleError}. The last displayed state is stale.</p> : null}{item.polls >= maxPolls && !terminal.has(item.status) ? <><p>Automatic account refresh stopped after {maxPolls} checks.</p><p className="field-help">The persisted job remains non-terminal. Continue refreshing this job or start a new collection; no terminal state is inferred.</p><button disabled={pending || activeCollectionId !== null} type="button" onClick={() => { setTrackedCollections(current => current.map(currentItem => currentItem.job_id === item.job_id ? { ...currentItem, polls: 0 } : currentItem)); setActiveCollectionId(item.job_id); }}>Continue refreshing {item.job_id}</button></> : null}{item.status === "needs_human" || item.status === "failed" ? <p className="field-help">Resolve the local xhs-cli session, captcha, rate-limit, or visibility issue outside the application, then create a new job. This job remains unchanged for audit.</p> : null}</li>)}
        {persistedXhsJobs.filter(job => !trackedCollections.some(item => item.job_id === job.id)).map(job => <li key={job.id}><header><strong>{job.state}</strong><code>{job.id}</code></header><p>{job.progress_current} / {job.progress_total ?? "unknown"} notes{job.error_category ? ` · ${job.error_category}` : ""}</p></li>)}
      </ol>}
    </section>

    <section className="operator-panel">
      <div className="panel-heading"><h2>Persisted public account snapshot</h2><p>{profile ? "1 profile" : "No profile"} · {notes.length} notes</p></div>
      {!profile ? <p className="panel-empty">No trusted account profile has been persisted. Run an exact account collection above.</p> : <div className="fact-list"><article><h3>{profile.nickname ?? profile.user_id}</h3>{profile.bio ? <p>{profile.bio}</p> : null}<p>Public stats: {Object.entries(profile.public_stats).map(([key, value]) => `${key} ${value}`).join(" · ") || "none returned"}</p><a href={profile.source_url} rel="noreferrer" target="_blank">Open profile source</a></article></div>}
      {notes.length === 0 ? <p className="panel-empty">No persisted public notes returned.</p> : <ol className="fact-list">{notes.map(note => <li key={`${note.user_id}:${note.note_id}`}><strong>{note.title ?? note.note_id}</strong>{note.summary ? <span>{note.summary}</span> : null}<span>{note.published_at ?? "Publish time not returned"} · {Object.entries(note.public_interactions).map(([key, value]) => `${key} ${value}`).join(" · ") || "No public interaction counts returned"}</span><a href={note.source_url} rel="noreferrer" target="_blank">Open note source</a></li>)}</ol>}
      {trustedAccountNoteEvidence.length ? <div><h3>Trusted selectable note evidence</h3><ul>{trustedAccountNoteEvidence.map(item => <li key={item.evidence_id}><code>{item.evidence_id}</code> · trusted account-note input</li>)}</ul><p className="field-help">Trusted account-note evidence can ground analysis claims. It does not replace exact shop N/N verification required for opportunity creation.</p></div> : null}
      {untrustedAccountNoteEvidence.length ? <div><h3>Notes requiring human verification</h3><ul>{untrustedAccountNoteEvidence.map(item => <li key={item.evidence_id}><code>{item.evidence_id}</code> · stale or untrusted · human verification required before analysis selection</li>)}</ul></div> : null}
    </section>

    <div className="workflow-grid">
      <section className="operator-panel"><div className="panel-heading"><h2>True-device shop verification</h2></div><form className="action-form" onSubmit={event => { event.preventDefault(); void act(async () => { const result = await queueShop({ account_user_id: account.user_id, account_name: account.account_name, expected_count: Number(expected), ...(device?.device_id ? { device_id: device.device_id } : {}), ...(verificationDir.trim() ? { verification_dir: verificationDir.trim() } : {}) }); try { await reloadFacts(); } catch { setActionError("Action completed, but persisted facts could not be reloaded."); } return `Queued job ${result.job_id}`; }); }}><label>Expected shop products<input aria-label="Expected shop products" min="0" required type="number" value={expected} onChange={event => setExpected(event.target.value)} /></label><label>Verification evidence directory<input aria-label="Verification evidence directory" placeholder="evidence/shops/account-id" value={verificationDir} onChange={event => setVerificationDir(event.target.value)} /></label><p className="field-help">Use a runtime-relative directory containing collection.json and exact N/N product evidence. Leave empty to queue collection and receive a factual verification-pending state.</p><button disabled={pending} type="submit">Queue device collection</button></form></section>
      <section className="operator-panel"><div className="panel-heading"><h2>Evidence-bound account report</h2></div>{evidence.length === 0 ? <p className="panel-empty">No eligible evidence returned.</p> : <form className="action-form" onSubmit={event => { event.preventDefault(); void act(async () => { await createAnalysis({ analysis_type: "account_report", account_user_id: account.user_id, account_user_ids: [], evidence_ids: selectedEvidenceIds }); try { await reloadFacts(); } catch { setActionError("Action completed, but persisted facts could not be reloaded."); } return "Account report completed. It is an observation signal only; cross-account candidates are created on Opportunities."; }); }}><fieldset><legend>Persisted evidence</legend>{evidence.map(item => { const selectable = item.kind !== "account_note" || item.eligible_for_opportunity; return <label className="check-label" key={item.evidence_id}><input aria-label={item.evidence_id} checked={selectable && selected.includes(item.evidence_id)} disabled={!selectable} type="checkbox" onChange={event => setSelected(current => event.target.checked ? [...current, item.evidence_id] : current.filter(id => id !== item.evidence_id))} />{item.evidence_id} · {item.kind}{item.kind === "account_note" && item.eligible_for_opportunity ? " · trusted selectable input" : item.kind === "account_note" ? " · stale or untrusted · human verification required" : item.eligible_for_opportunity ? " · deep-verified" : ""}</label>; })}</fieldset><p className="field-help">A single account can produce observations only. Select at least two complete accounts on Opportunities for demand validation.</p><div className="button-row"><button disabled={pending || selectedEvidenceIds.length === 0} type="submit">Generate account report</button></div></form>}</section>
    </div>
    <section className="operator-panel"><div className="panel-heading"><h2>Collection jobs</h2><p>{deviceJobs.length} returned</p></div>{deviceJobs.length === 0 ? <p className="panel-empty">No device collection has been recorded for this account.</p> : <ol className="collection-list">{deviceJobs.map(job => { const missing = shopMissingItems(job); return <li key={job.id}><header><strong>{job.state === "needs_human" ? "Human attention required" : job.state}</strong><span>{job.progress_current} / {job.progress_total ?? "unknown"} verified or collected</span></header><p>Stage: {job.current_stage ?? "not reported"}{job.error_category ? ` · Error: ${job.error_category}` : ""}</p>{missing.length ? <><h3>Reported missing items</h3><ul>{missing.map((item, index) => <li key={`${item.reference}-${index}`}>{item.reference} · {item.reason}</li>)}</ul></> : null}<a href={`/jobs#job-${encodeURIComponent(job.id)}-evidence`}>Inspect {job.artifacts.length} evidence {job.artifacts.length === 1 ? "item" : "items"}</a>{job.state === "needs_human" || job.state === "failed" ? <p className="field-help">Resolve the device, login, or evidence issue, then queue a new collection above. The prior job remains unchanged for audit.</p> : null}</li>; })}</ol>}</section>
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
