import { useCallback, useEffect, useState } from "react";

import { fetchAccounts, fetchRankSnapshots, ingestRankSnapshot as postSnapshot, type Account, type RankSnapshot } from "../api/client";

type RadarData = { snapshots: RankSnapshot[]; accounts: Account[] };
export interface RadarPageProps { loadRadar?: () => Promise<RadarData>; ingestSnapshot?: (payload: Record<string, unknown>) => Promise<Partial<RankSnapshot> & { id: number }>; }

const defaultLoad = async (): Promise<RadarData> => {
  const [snapshots, accounts] = await Promise.all([fetchRankSnapshots(), fetchAccounts()]);
  return { snapshots, accounts };
};

export function RadarPage({ loadRadar = defaultLoad, ingestSnapshot = postSnapshot }: RadarPageProps) {
  const [state, setState] = useState<{ kind: "loading" } | { kind: "error" } | { kind: "ready"; data: RadarData }>({ kind: "loading" });
  const [snapshotJson, setSnapshotJson] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    setState({ kind: "loading" });
    try { setState({ kind: "ready", data: await loadRadar() }); } catch { setState({ kind: "error" }); }
  }, [loadRadar]);
  useEffect(() => { void refresh(); }, [refresh]);

  if (state.kind === "loading") return <main className="workbench-page" id="main-content"><section className="loading-panel" aria-busy="true"><p role="status">Loading demand radar</p></section></main>;
  if (state.kind === "error") return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>Could not load demand radar</h1><p>The persisted ranking endpoints did not return a result.</p><button type="button" onClick={() => void refresh()}>Retry demand radar</button></section></main>;
  const { snapshots, accounts } = state.data;
  return <main className="workbench-page" id="main-content">
    <header className="page-heading page-heading--split"><div><p className="eyebrow">Ranking evidence</p><h1>Demand radar</h1><p>Persisted Qianfan snapshots and explainable account scores.</p></div><p className="result-count" role="status">{snapshots.length} snapshots · {accounts.length} accounts</p></header>
    <section className="operator-panel"><div className="panel-heading"><h2>Import captured ranking snapshot</h2><p>Existing persisted API</p></div><form className="action-form" onSubmit={event => { event.preventDefault(); setActionError(null); void (async () => { try { const parsed = JSON.parse(snapshotJson) as unknown; if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Snapshot JSON must be one object."); const result = await ingestSnapshot(parsed as Record<string, unknown>); setNotice(`Snapshot ${result.id} persisted. Persisted facts were reloaded.`); setState({ kind: "ready", data: await loadRadar() }); } catch (error) { setActionError(error instanceof Error ? error.message : "Snapshot import failed."); } })(); }}><label>Captured ranking snapshot JSON<textarea aria-label="Captured ranking snapshot JSON" required value={snapshotJson} onChange={event => setSnapshotJson(event.target.value)} /></label><p className="field-help">Paste a real captured RankSnapshotInput object. This imports evidence through the existing API; it does not start the Playwright collector.</p><button type="submit">Import captured snapshot</button></form></section>
    {notice ? <p className="action-notice" role="status">{notice}</p> : null}{actionError ? <p className="action-error" role="alert">{actionError}</p> : null}
    {snapshots.length === 0 && accounts.length === 0 ? <section className="message-panel" role="status"><h2>No ranking evidence recorded</h2><p>Run a controlled ranking collection or submit a real captured snapshot through the API. This page never inserts sample data.</p></section> : <>
      <section className="operator-panel" aria-labelledby="snapshots-heading"><div className="panel-heading"><h2 id="snapshots-heading">Ranking snapshots</h2></div>{snapshots.length === 0 ? <p className="panel-empty">No snapshots returned.</p> : <ol className="fact-list">{snapshots.map(snapshot => <li key={snapshot.id}><strong>{snapshot.board} · {snapshot.dimension}</strong><span>{snapshot.source_date} · {snapshot.deduplicated_count} deduplicated / {snapshot.submitted_count} submitted</span><a href={snapshot.source_url}>Source evidence</a></li>)}</ol>}</section>
      <section className="operator-panel" aria-labelledby="accounts-heading"><div className="panel-heading"><h2 id="accounts-heading">Scored accounts</h2></div>{accounts.length === 0 ? <p className="panel-empty">No scoreable accounts returned.</p> : <div className="table-scroll"><table><thead><tr><th>Account</th><th>Score</th><th>Fans</th><th>Evidence</th><th>Coverage</th></tr></thead><tbody>{accounts.map(account => <tr key={account.user_id}><th><a href={`/accounts/${encodeURIComponent(account.user_id)}`}>{account.account_name}</a><small>{account.user_id}</small></th><td>{account.score}</td><td>{account.fans}</td><td>{account.gmv} · {account.pay} · {account.read}</td><td>{account.nday} days · {account.nboard} boards</td></tr>)}</tbody></table></div>}</section>
    </>}
  </main>;
}
