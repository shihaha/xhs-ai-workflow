import { useCallback, useEffect, useRef, useState } from "react";

import { advanceCandidateFunnel as postAdvanceCandidate, fetchAccounts, fetchCandidateFunnel, fetchHealth, fetchJob, fetchJobs, fetchNoteSearchResults, fetchRankSnapshots, ingestRankSnapshot as postSnapshot, runCandidatePrescreen as postCandidatePrescreen, startNoteSearch as postNoteSearch, startQianfanCollection as postCollection, type Account, type CandidateAdvanceQueued, type CandidateFunnel, type CollectionQueued, type HealthResponse, type Job, type NoteSearchResults, type QianfanCollectionQueued, type RankSnapshot } from "../api/client";

type RadarData = { snapshots: RankSnapshot[]; accounts: Account[]; funnel?: CandidateFunnel[]; health?: HealthResponse };
type TrackedSearch = { job_id: string; status: Job["state"]; polls: number; job?: Job; staleError?: string; results?: NoteSearchResults };
export interface RadarPageProps {
  loadRadar?: () => Promise<RadarData>;
  ingestSnapshot?: (payload: Record<string, unknown>) => Promise<Partial<RankSnapshot> & { id: number }>;
  startCollection?: (payload: { expected_count_per_scope: number }) => Promise<QianfanCollectionQueued>;
  loadCollectionJobs?: () => Promise<Job[]>;
  startNoteSearch?: (payload: { keyword: string; expected_count: number }) => Promise<CollectionQueued>;
  loadSearchJob?: (jobId: string) => Promise<Job>;
  loadSearchResults?: (jobId: string) => Promise<NoteSearchResults>;
  runPrescreen?: (payload: { source_date: string; limit: number }) => Promise<CandidateFunnel[]>;
  advanceCandidate?: (payload: { source_date: string }) => Promise<CandidateAdvanceQueued>;
  pollIntervalMs?: number;
  searchMaxPolls?: number;
}

const defaultLoad = async (): Promise<RadarData> => {
  const [snapshots, accounts, health] = await Promise.all([fetchRankSnapshots(), fetchAccounts(), fetchHealth()]);
  const latestSourceDate = snapshots.map(item => item.source_date).sort().at(-1);
  const funnel = latestSourceDate ? await fetchCandidateFunnel(latestSourceDate) : [];
  return { snapshots, accounts, funnel, health };
};

const terminal = new Set(["needs_human", "succeeded", "failed", "cancelled"]);

const prescreenLabel = (value: CandidateFunnel["prescreen_classification"]) => ({
  pending: "待范围预筛",
  likely_digital: "疑似数字",
  clearly_physical: "明显实体",
  uncertain: "无法判断",
}[value]);

const candidateStatusLabel = (value: CandidateFunnel["status"]) => ({
  pending_prescreen: "待范围预筛",
  clearly_physical_skipped: "明显实体，已跳过",
  likely_digital_waiting_preflight: "疑似数字，等待 Android preflight",
  uncertain_waiting_preflight: "无法判断，等待 Android preflight",
  preflight_active: "Android preflight 进行中",
  in_scope: "已确认 in_scope",
  out_of_scope_physical: "已确认 out_of_scope_physical",
  needs_human: "needs_human",
  collection_failed: "真实采集失败，继续补位",
}[value]);

export function RadarPage({ loadRadar = defaultLoad, ingestSnapshot = postSnapshot, startCollection = postCollection, loadCollectionJobs = fetchJobs, startNoteSearch = postNoteSearch, loadSearchJob = fetchJob, loadSearchResults = fetchNoteSearchResults, runPrescreen = postCandidatePrescreen, advanceCandidate = postAdvanceCandidate, pollIntervalMs = 1000, searchMaxPolls = 60 }: RadarPageProps) {
  const [state, setState] = useState<{ kind: "loading" } | { kind: "error" } | { kind: "ready"; data: RadarData }>({ kind: "loading" });
  const [snapshotJson, setSnapshotJson] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [expected, setExpected] = useState("1");
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const [collection, setCollection] = useState<QianfanCollectionQueued | null>(null);
  const [scopeJobs, setScopeJobs] = useState<Job[]>([]);
  const [pollCount, setPollCount] = useState(0);
  const [pollError, setPollError] = useState<string | null>(null);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [searchExpected, setSearchExpected] = useState("2");
  const [searches, setSearches] = useState<TrackedSearch[]>([]);
  const [activeSearchId, setActiveSearchId] = useState<string | null>(null);
  const mounted = useRef(true);
  const refresh = useCallback(async () => {
    setState({ kind: "loading" });
    try { setState({ kind: "ready", data: await loadRadar() }); } catch { setState({ kind: "error" }); }
  }, [loadRadar]);
  useEffect(() => { mounted.current = true; void refresh(); return () => { mounted.current = false; }; }, [refresh]);
  const refreshScopes = useCallback(async () => {
    if (!collection) return;
    const ids = new Set(collection.scopes.map(scope => scope.job_id));
    const jobs = (await loadCollectionJobs()).filter(job => ids.has(job.id));
    if (mounted.current) { setScopeJobs(jobs); setPollError(null); }
  }, [collection, loadCollectionJobs]);
  useEffect(() => {
    if (!collection || pollCount >= 10 || (scopeJobs.length === 8 && scopeJobs.every(job => terminal.has(job.state)))) return;
    let cancelled = false;
    const timer = window.setTimeout(() => { void refreshScopes().catch(error => { if (!cancelled && mounted.current) setPollError(error instanceof Error ? error.message : "Scope job refresh failed."); }).finally(() => { if (!cancelled && mounted.current) setPollCount(value => value + 1); }); }, pollIntervalMs);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [collection, pollCount, pollIntervalMs, refreshScopes, scopeJobs]);

  const activeSearch = searches.find(item => item.job_id === activeSearchId);
  useEffect(() => {
    if (!activeSearchId || !activeSearch || terminal.has(activeSearch.status)) return;
    if (activeSearch.polls >= searchMaxPolls) {
      setActiveSearchId(current => current === activeSearchId ? null : current);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void loadSearchJob(activeSearchId).then(async job => {
        if (cancelled || !mounted.current) return;
        let results: NoteSearchResults | undefined;
        if (job.state === "succeeded") results = await loadSearchResults(activeSearchId);
        if (cancelled || !mounted.current) return;
        setSearches(current => current.map(item => item.job_id === activeSearchId ? { ...item, status: job.state, job, results, polls: item.polls + 1, staleError: undefined } : item));
        if (terminal.has(job.state)) setActiveSearchId(null);
      }).catch(error => {
        if (cancelled || !mounted.current) return;
        const message = error instanceof Error ? error.message : "Search job read failed.";
        setSearches(current => current.map(item => item.job_id === activeSearchId ? { ...item, polls: item.polls + 1, staleError: message } : item));
      });
    }, pollIntervalMs);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [activeSearch, activeSearchId, loadSearchJob, loadSearchResults, pollIntervalMs, searchMaxPolls]);

  const singleFlight = async (action: () => Promise<void>) => {
    if (pendingRef.current) return;
    pendingRef.current = true; setPending(true); setActionError(null);
    try { await action(); } catch (error) { setActionError(error instanceof Error ? error.message : "Action failed."); }
    finally { pendingRef.current = false; if (mounted.current) setPending(false); }
  };

  if (state.kind === "loading") return <main className="workbench-page" id="main-content"><section className="loading-panel" aria-busy="true"><p role="status">Loading demand radar</p></section></main>;
  if (state.kind === "error") return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>无法加载需求雷达</h1><p>已保存的榜单数据接口没有返回有效结果。</p><button type="button" onClick={() => void refresh()}>重新加载需求雷达</button></section></main>;
  const { snapshots, accounts, funnel = [] } = state.data;
  const latestSourceDate = snapshots.map(item => item.source_date).sort().at(-1);
  const succeeded = scopeJobs.filter(job => job.state === "succeeded").length;
  const stateCounts = scopeJobs.reduce<Record<string, number>>((counts, job) => ({ ...counts, [job.state]: (counts[job.state] ?? 0) + 1 }), {});
  const profile = scopeJobs.map(job => job.input.selector_profile_version).find(value => typeof value === "string") as string | undefined;
  const playwright = state.data.health?.checks.browser;
  return <main className="workbench-page" id="main-content">
    <header className="page-heading page-heading--split"><div><p className="eyebrow">Ranking evidence</p><h1>Demand radar</h1><p>Persisted Qianfan snapshots and explainable account scores.</p></div><p className="result-count" role="status">{snapshots.length} snapshots · {accounts.length} accounts</p></header>
    <section className="operator-panel"><div className="panel-heading"><h2>Automatic Qianfan collection</h2><p>Playwright collector · fixed eight scopes</p></div>
      <p className={`inline-status state--${playwright?.healthy ? "available" : "unavailable"}`}>Browser executable {playwright?.healthy ? "available" : "unavailable"}{playwright?.executable ? `: ${playwright.executable}` : ""}. This health fact does not prove Playwright or a persistent Qianfan login profile is ready. Overall API health: {state.data.health?.status ?? "not returned"}.</p>
      <form className="action-form" onSubmit={event => { event.preventDefault(); void singleFlight(async () => { const count = Number(expected); if (!Number.isInteger(count) || count < 1 || count > 1000) throw new Error("Expected rows must be a whole number from 1 to 1000."); const queued = await startCollection({ expected_count_per_scope: count }); const ids = new Set(queued.scopes.map(scope => scope.job_id)); setCollection(queued); setScopeJobs((await loadCollectionJobs()).filter(job => ids.has(job.id))); setPollCount(0); setNotice(`Collection ${queued.collection_id} reserved as one batch of ${queued.scopes.length} scope jobs.`); }); }}><label>Expected rows per ranking scope<input aria-label="Expected rows per ranking scope" min="1" max="1000" step="1" required type="number" value={expected} onChange={event => setExpected(event.target.value)} /></label><p className="field-help">Starts the real collector for four boards × two dimensions. Completion requires all eight persisted jobs to succeed.</p><button disabled={pending} type="submit">Start automatic Qianfan collection</button></form>
      {collection ? <div className="collection-summary"><h3>Collection {collection.collection_id}</h3><p>Batch: {collection.scopes.length} reserved scope jobs; the API returns no separate batch ID.</p><p>{succeeded === 8 && scopeJobs.length === 8 ? "Complete: 8/8 scopes succeeded." : `Not complete: ${succeeded}/8 scopes succeeded${Object.entries(stateCounts).filter(([key]) => key !== "succeeded").map(([key, count]) => `; ${count} ${key}`).join("")}.`}</p><p>Selector profile: {profile ?? "waiting for persisted job facts"}. Profile status: {profile?.includes("unverified") ? "unverified" : profile ? "not asserted by API" : "waiting"}.</p>{pollError ? <p className="action-error" role="alert">Scope refresh failed: {pollError}. Persisted states shown above may be stale; retry manually.</p> : null}{pollCount >= 10 && !(scopeJobs.length === 8 && scopeJobs.every(job => terminal.has(job.state))) ? <p>Automatic refresh stopped after 10 checks.</p> : null}<button disabled={pending} type="button" onClick={() => void singleFlight(refreshScopes)}>Refresh eight scope jobs</button><ol className="collection-list">{collection.scopes.map(scope => { const job = scopeJobs.find(item => item.id === scope.job_id); return <li key={scope.job_id}><strong>{scope.board} · {scope.dimension}</strong><span>{job?.state ?? scope.status} · {job?.progress_current ?? 0} / {job?.progress_total ?? "waiting"}</span>{job?.error_category ? <span>Error: {job.error_category}</span> : null}<code>{scope.job_id}</code></li>; })}</ol></div> : null}
    </section>
    <section className="operator-panel">
      <div className="panel-heading"><h2>Public note keyword search</h2><p>Local xhs-cli · normalized public facts only</p></div>
      <form className="action-form" onSubmit={event => { event.preventDefault(); void singleFlight(async () => {
        const count = Number(searchExpected);
        if (!Number.isInteger(count) || (count !== 2 && (count < 5 || count > 10))) throw new Error("Use 2 for the first pass or 5 to 10 for the target.");
        const keyword = searchKeyword.trim();
        if (!keyword) throw new Error("A note search keyword is required.");
        const queued = await startNoteSearch({ keyword, expected_count: count });
        if (!mounted.current) return;
        setSearches(current => [...current, { job_id: queued.job_id, status: queued.status, polls: 0 }]);
        setActiveSearchId(queued.job_id);
        setNotice(`Note search ${queued.job_id} queued.`);
      }); }}>
        <label>Note search keyword<input aria-label="Note search keyword" maxLength={500} required value={searchKeyword} onChange={event => setSearchKeyword(event.target.value)} /></label>
        <label>First-pass public notes per keyword<input aria-label="First-pass public notes per keyword" min="2" max="10" step="1" required type="number" value={searchExpected} onChange={event => setSearchExpected(event.target.value)} /></label>
        <p className="field-help">The tutorial workflow targets 5–10 qualifying notes per actual keyword. The first pass collects 2 complete notes to verify evidence quality; later passes stop as soon as the target is met and deduplicate the same product across keywords.</p>
        <p className="field-help">Operator prerequisite: authenticate the trusted local xhs-cli session outside this application. Search accepts no Cookie, token, password, executable path, URL, or login action.</p>
        <button disabled={pending || activeSearchId !== null} type="submit">Search public notes</button>
      </form>
      {searches.length === 0 ? <p className="panel-empty">No keyword search has been started in this page session.</p> : <ol className="collection-list">{searches.map(item => <li key={item.job_id}>
        <header><strong>{item.status === "needs_human" ? "Human attention required" : item.status}</strong><code>{item.job_id}</code></header>
        <p>{item.job ? `${item.job.progress_current} / ${item.job.progress_total ?? "unknown"} public notes · ${item.job.current_stage ?? "stage not reported"}` : "Waiting for the first persisted job read."}{item.job?.error_category ? ` · ${item.job.error_category}` : ""}</p>
        {item.staleError ? <p className="action-error" role="alert">Search job read failed: {item.staleError}. The last displayed state is stale.</p> : null}
        {item.polls >= searchMaxPolls && !terminal.has(item.status) ? <><p>Automatic search refresh stopped after {searchMaxPolls} checks.</p><p className="field-help">The persisted job remains non-terminal. Continue refreshing this job or start a new search; no terminal state is inferred.</p><button disabled={pending || activeSearchId !== null} type="button" onClick={() => { setSearches(current => current.map(currentItem => currentItem.job_id === item.job_id ? { ...currentItem, polls: 0 } : currentItem)); setActiveSearchId(item.job_id); }}>Continue refreshing {item.job_id}</button></> : null}
        {item.status === "needs_human" || item.status === "failed" ? <p className="field-help">Resolve the local xhs-cli session, captcha, rate-limit, or visibility issue outside the application, then create a new search job. This job remains unchanged for audit.</p> : null}
        {item.results ? <div><p>{item.results.succeeded_count} / {item.results.expected_count} public notes returned</p>{item.results.items.length === 0 ? <p className="panel-empty">The exact search completed with no public notes.</p> : <ol className="fact-list">{item.results.items.map(note => <li key={note.note_id}><strong>{note.title ?? note.note_id}</strong>{note.summary ? <span>{note.summary}</span> : null}{note.user_id ? <span>Account {note.user_id}</span> : null}<a href={note.source_url} rel="noreferrer" target="_blank">Open search result</a></li>)}</ol>}</div> : null}
      </li>)}</ol>}
    </section>
    <section className="operator-panel"><div className="panel-heading"><h2>Import captured ranking snapshot</h2><p>Manual evidence import · not automatic collection</p></div><form className="action-form" onSubmit={event => { event.preventDefault(); void singleFlight(async () => { const parsed = JSON.parse(snapshotJson) as unknown; if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Snapshot JSON must be one object."); const result = await ingestSnapshot(parsed as Record<string, unknown>); setNotice(`Snapshot ${result.id} persisted. Persisted facts were reloaded.`); setState({ kind: "ready", data: await loadRadar() }); }); }}><label>Captured ranking snapshot JSON<textarea aria-label="Captured ranking snapshot JSON" required value={snapshotJson} onChange={event => setSnapshotJson(event.target.value)} /></label><p className="field-help">Paste a real captured RankSnapshotInput object. This imports evidence through the existing API; it does not start the Playwright collector.</p><button disabled={pending} type="submit">Import captured snapshot</button></form></section>
    {notice ? <p className="action-notice" role="status">{notice}</p> : null}{actionError ? <p className="action-error" role="alert">{actionError}</p> : null}
    <section className="operator-panel" aria-labelledby="candidate-funnel-heading">
      <div className="panel-heading"><h2 id="candidate-funnel-heading">Phase A 候选业务范围漏斗</h2><p>低成本预筛 ≠ 真实店铺最终核验</p></div>
      <p className="field-help">候选始终按教程原评分顺序推进。明显实体账号在 Android 前跳过；疑似数字和无法判断的账号仍必须进入原有 Android preflight。这里不会定向寻找同类账号，也不会改变单店前 3 件商品规则。</p>
      <div className="button-row"><button disabled={pending || !latestSourceDate} type="button" onClick={() => void singleFlight(async () => { if (!latestSourceDate) throw new Error("没有可用于预筛的真实千帆日期。"); const rows = await runPrescreen({ source_date: latestSourceDate, limit: 1000 }); setState({ kind: "ready", data: { ...state.data, funnel: rows } }); setNotice(`已按评分顺序完成 ${rows.length} 个候选的低成本预筛。`); })}>按评分顺序执行低成本预筛</button><button disabled={pending || !latestSourceDate} type="button" onClick={() => void singleFlight(async () => { if (!latestSourceDate) throw new Error("没有可用于补位的真实千帆日期。"); const queued = await advanceCandidate({ source_date: latestSourceDate }); setNotice(`第 ${queued.candidate_position} 名候选已进入 Android preflight：${queued.job_id}`); setState({ kind: "ready", data: await loadRadar() }); })}>处理下一名候选</button></div>
      {funnel.length === 0 ? <p className="panel-empty">尚未形成候选范围漏斗。先对当前真实排名池执行低成本预筛。</p> : <ol className="collection-list">{funnel.map(candidate => <li key={candidate.user_id}><header><strong>{candidate.account_name}</strong><span>{candidateStatusLabel(candidate.status)}</span></header><p>第 {candidate.candidate_position} 名 · 千帆原始 best rank {candidate.best_rank} · 原始评分 {candidate.score_status === "scored" ? candidate.score : "指标不足"}</p><p><strong>低成本预筛：</strong> {prescreenLabel(candidate.prescreen_classification)}</p><p>{candidate.prescreen_reason ?? "尚未执行预筛"}</p><p><strong>预筛证据：</strong> {candidate.prescreen_evidence_ids.length ? candidate.prescreen_evidence_ids.join(", ") : "尚无"}</p><p><strong>最终店铺核验：</strong> {candidate.android_scope_classification === "unknown" ? "尚未执行" : candidate.android_scope_classification} · Android job {candidate.android_job_state}</p><a href={`/accounts/${encodeURIComponent(candidate.user_id)}`}>查看账号证据</a></li>)}</ol>}
    </section>
    {snapshots.length === 0 && accounts.length === 0 ? <section className="message-panel" role="status"><h2>No ranking evidence recorded</h2><p>Run a controlled ranking collection or submit a real captured snapshot through the API. This page never inserts sample data.</p></section> : <>
      <section className="operator-panel" aria-labelledby="snapshots-heading"><div className="panel-heading"><h2 id="snapshots-heading">Ranking snapshots</h2></div>{snapshots.length === 0 ? <p className="panel-empty">No snapshots returned.</p> : <ol className="fact-list">{snapshots.map(snapshot => <li key={snapshot.id}><strong>{snapshot.board} · {snapshot.dimension}</strong><span>{snapshot.source_date} · {snapshot.deduplicated_count} deduplicated / {snapshot.submitted_count} submitted</span><a href={snapshot.source_url}>Source evidence</a></li>)}</ol>}</section>
      <section className="operator-panel" aria-labelledby="accounts-heading"><div className="panel-heading"><h2 id="accounts-heading">Ranked account candidates</h2></div>{accounts.length === 0 ? <p className="panel-empty">No ranked accounts returned.</p> : <div className="table-scroll"><table><thead><tr><th>Account</th><th>Score</th><th>Fans</th><th>Evidence</th><th>Coverage</th></tr></thead><tbody>{accounts.map(account => <tr key={account.user_id}><th><a href={`/accounts/${encodeURIComponent(account.user_id)}`}>{account.account_name}</a><small>{account.user_id}</small></th><td>{account.score_status === "insufficient_metrics" ? "insufficient_metrics" : account.score}</td><td>{account.fans}</td><td>{account.gmv} · {account.pay} · {account.read}</td><td>{account.ranking_evidence_count} appearances · best rank {account.best_rank} · {account.nday} days · {account.nboard} boards</td></tr>)}</tbody></table></div>}</section>
    </>}
  </main>;
}
