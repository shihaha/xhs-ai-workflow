import { useCallback, useEffect, useState } from "react";

import {
  cancelAgentJob,
  denyAgentHumanAction,
  fetchAgentHumanActions,
  fetchAgentJob,
  fetchAgentJobs,
  fetchAgentRun,
  fetchAgentRuns,
  fetchChatGPTHandoffs,
  type AgentHumanAction,
  type AgentHumanActionDecision,
  type AgentJobCancelResult,
  type AgentJobRuntime,
  type AgentJobSummary,
  type AgentRunDetail,
  type AgentRunListItem,
  type ChatGPTHandoffTask,
} from "../api/client";

type ResourceState<T> =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "ready"; value: T };

export interface AgentWorkbenchPageProps {
  loadJobs?: () => Promise<AgentJobSummary[]>;
  loadRuns?: () => Promise<AgentRunListItem[]>;
  loadHumanActions?: () => Promise<AgentHumanAction[]>;
  loadHandoffs?: () => Promise<ChatGPTHandoffTask[]>;
  denyAction?: (actionId: string, payload: { note?: string }) => Promise<AgentHumanActionDecision>;
}

interface AgentWorkbenchData {
  jobs: AgentJobSummary[];
  runs: AgentRunListItem[];
  humanActions: AgentHumanAction[];
  handoffs: ChatGPTHandoffTask[];
}

export function AgentWorkbenchPage({
  loadJobs = fetchAgentJobs,
  loadRuns = fetchAgentRuns,
  loadHumanActions = fetchAgentHumanActions,
  loadHandoffs = fetchChatGPTHandoffs,
  denyAction = denyAgentHumanAction,
}: AgentWorkbenchPageProps) {
  const [resource, setResource] = useState<ResourceState<AgentWorkbenchData>>({ kind: "loading" });

  const refresh = useCallback(async () => {
    setResource({ kind: "loading" });
    try {
      const [jobs, runs, humanActions, handoffs] = await Promise.all([
        loadJobs(),
        loadRuns(),
        loadHumanActions(),
        loadHandoffs(),
      ]);
      setResource({ kind: "ready", value: { jobs, runs, humanActions, handoffs } });
    } catch {
      setResource({ kind: "error" });
    }
  }, [loadHandoffs, loadHumanActions, loadJobs, loadRuns]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (resource.kind === "loading") return <AgentLoading label="Loading Agent workbench" />;
  if (resource.kind === "error") return <AgentLoadError onRetry={refresh} />;
  const deny = async (actionId: string) => {
    await denyAction(actionId, {});
    await refresh();
  };
  return <AgentWorkbenchView data={resource.value} onDeny={deny} />;
}

export function AgentWorkbenchView({ data, onDeny }: { data: AgentWorkbenchData; onDeny?: (actionId: string) => Promise<void> }) {
  const pendingHuman = data.humanActions.filter((item) => item.status === "pending").length;
  const activeChatGPT = data.handoffs.filter((item) => item.needs_chatgpt).length;

  return (
    <main className="workbench-page" id="main-content">
      <header className="page-heading page-heading--split">
        <div>
          <p className="eyebrow">Durable Agent orchestration</p>
          <h1>Agent 工作台</h1>
          <p>展示 durable Job/Run/HumanAction 状态；有限 operator 命令提交给后端重新校验，不由 React 修改 lifecycle。</p>
        </div>
        <p className="boundary-note">Backend-authoritative · continuation remains fail-closed</p>
      </header>

      <section aria-label="Agent workbench summary" className="agent-summary-grid">
        <SummaryCard label="Jobs" value={data.jobs.length} />
        <SummaryCard label="Agent Runs" value={data.runs.length} />
        <SummaryCard label="Pending Human Actions" value={pendingHuman} />
        <SummaryCard label="需要 ChatGPT 处理" value={activeChatGPT} />
      </section>

      <HandoffSection handoffs={data.handoffs} />
      <JobsSection jobs={data.jobs} />
      <RunsSection runs={data.runs} />
      <HumanActionsSection actions={data.humanActions} onDeny={onDeny} />
    </main>
  );
}

function SummaryCard({ label, value }: { label: string; value: number }) {
  return (
    <article className="agent-summary-card">
      <p>{label}</p>
      <strong>{value}</strong>
    </article>
  );
}

function HandoffSection({ handoffs }: { handoffs: ChatGPTHandoffTask[] }) {
  return (
    <section className="operator-panel" aria-labelledby="agent-handoffs-heading">
      <div className="panel-heading">
        <div>
          <h2 id="agent-handoffs-heading">ChatGPT handoff tasks</h2>
          <p>显示 durable identity / lifecycle；不暴露 task/result 原文或本机路径。</p>
        </div>
        <span>{handoffs.length} tasks</span>
      </div>
      {handoffs.length === 0 ? (
        <p className="panel-empty">当前没有 durable ChatGPT handoff。</p>
      ) : (
        <ol className="agent-record-list">
          {handoffs.map((handoff) => (
            <li className="agent-record" key={handoff.handoff_id}>
              <header>
                <div>
                  <h3>Handoff <code>{handoff.handoff_id}</code></h3>
                  <p>Job <a href={`/agent/jobs/${encodeURIComponent(handoff.job_id)}`}><code>{handoff.job_id}</code></a></p>
                </div>
                <HandoffBadge handoff={handoff} />
              </header>
              <dl className="agent-facts">
                <Fact term="Handoff" value={handoff.status} />
                <Fact term="HumanAction" value={handoff.human_action_status} />
                <Fact term="Job state" value={handoff.job_state} />
                <Fact term="Stage" value={handoff.current_stage ?? "—"} />
                <Fact term="Current binding" value={handoff.is_current_binding ? "yes" : "no"} />
                <Fact term="Context refs" value={String(handoff.context_ref_count)} />
              </dl>
              <p className="agent-identity-line">schema <code>{handoff.schema_version}</code> · revision <code>{handoff.stage_revision}</code> · input <code>{handoff.input_hash}</code></p>
              {handoff.authority_ambiguous ? (
                <p className="action-error">Authority is ambiguous. Workbench must not infer permission to continue.</p>
              ) : handoff.result_ready ? (
                <p className="action-notice">结果已交回并持久化；Job 仍等待后端安全 continuation，不由前端恢复。</p>
              ) : handoff.needs_chatgpt ? (
                <p className="inline-status">需要 ChatGPT 处理；该标记来自后端严格 authority projection。</p>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function HandoffBadge({ handoff }: { handoff: ChatGPTHandoffTask }) {
  if (handoff.authority_ambiguous) return <span className="agent-badge agent-badge--danger">authority ambiguous</span>;
  if (handoff.needs_chatgpt) return <span className="agent-badge agent-badge--attention">需要 ChatGPT 处理</span>;
  if (handoff.result_ready) return <span className="agent-badge agent-badge--ready">已交回 / result ready</span>;
  return <span className="agent-badge">{handoff.status}</span>;
}

function JobsSection({ jobs }: { jobs: AgentJobSummary[] }) {
  return (
    <section className="operator-panel" aria-labelledby="agent-jobs-heading">
      <div className="panel-heading">
        <div>
          <h2 id="agent-jobs-heading">Jobs</h2>
          <p>Job 是 operator/task lifecycle authority；counts 均来自 durable projection。</p>
        </div>
        <span>{jobs.length} jobs</span>
      </div>
      {jobs.length === 0 ? <p className="panel-empty">当前没有 Agent orchestration Jobs。</p> : (
        <div className="table-scroll">
          <table>
            <thead><tr><th>Job</th><th>State / stage</th><th>Current run</th><th>Durable records</th><th>Updated</th></tr></thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.job_id}>
                  <th scope="row"><a href={`/agent/jobs/${encodeURIComponent(job.job_id)}`}><code>{job.job_id}</code></a>{job.authority_ambiguous ? <small>authority ambiguous</small> : null}</th>
                  <td><strong>{job.job_state}</strong><br /><span>{job.current_stage ?? "no stage"}</span></td>
                  <td>{job.current_run_id ? <a href={`/agent/runs/${encodeURIComponent(job.current_run_id)}`}><code>{job.current_run_id}</code></a> : "—"}</td>
                  <td>{job.run_count} runs · {job.pending_human_action_count} pending human · {job.evidence_count} evidence · {job.artifact_count} artifacts</td>
                  <td><time dateTime={job.updated_at}>{job.updated_at}</time></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function RunsSection({ runs }: { runs: AgentRunListItem[] }) {
  return (
    <section className="operator-panel" aria-labelledby="agent-runs-heading">
      <div className="panel-heading"><div><h2 id="agent-runs-heading">Agent Runs</h2><p>Execution traces bound to durable Jobs.</p></div><span>{runs.length} runs</span></div>
      {runs.length === 0 ? <p className="panel-empty">当前没有 bound AgentRun。</p> : (
        <ol className="agent-record-list">
          {runs.map((run) => (
            <li className="agent-record agent-record--compact" key={run.run_id}>
              <header>
                <div><h3><a href={`/agent/runs/${encodeURIComponent(run.run_id)}`}><code>{run.run_id}</code></a></h3><p>{run.goal}</p></div>
                <span className="agent-badge">{run.state}</span>
              </header>
              <p className="agent-identity-line">Job <a href={`/agent/jobs/${encodeURIComponent(run.job_id)}`}><code>{run.job_id}</code></a> · {run.step_count} steps · {run.model_calls} model calls · {run.input_tokens + run.output_tokens} tokens</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function HumanActionsSection({ actions, onDeny }: { actions: AgentHumanAction[]; onDeny?: (actionId: string) => Promise<void> }) {
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const deny = async (actionId: string) => {
    if (!onDeny) return;
    setBusyId(actionId);
    setError(null);
    try {
      await onDeny(actionId);
      setConfirmingId(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "HumanAction denial failed.");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="operator-panel" aria-labelledby="agent-human-heading">
      <div className="panel-heading"><div><h2 id="agent-human-heading">Human Actions</h2><p>只展示身份、工具和 durable lifecycle，不展示 request/resolution JSON。</p></div><span>{actions.length} actions</span></div>
      {error ? <p className="action-error" role="alert">{error}</p> : null}
      {actions.length === 0 ? <p className="panel-empty">当前没有 HumanAction。</p> : (
        <div className="table-scroll">
          <table>
            <thead><tr><th>Action</th><th>Status</th><th>Tool</th><th>Job / Run</th><th>Created</th><th>Operator</th></tr></thead>
            <tbody>
              {actions.map((action) => (
                <tr key={action.id}>
                  <th scope="row"><code>{action.id}</code></th>
                  <td><strong>{action.status}</strong></td>
                  <td>{action.tool_name}</td>
                  <td><a href={`/agent/jobs/${encodeURIComponent(action.job_id)}`}><code>{action.job_id}</code></a><br /><a href={`/agent/runs/${encodeURIComponent(action.run_id)}`}><code>{action.run_id}</code></a></td>
                  <td><time dateTime={action.created_at}>{action.created_at}</time></td>
                  <td>{action.can_deny && onDeny ? (
                    confirmingId === action.id ? (
                      <span className="button-row">
                        <button disabled={busyId === action.id} onClick={() => void deny(action.id)} type="button">确认拒绝并终止</button>
                        <button disabled={busyId === action.id} onClick={() => setConfirmingId(null)} type="button">返回</button>
                      </span>
                    ) : <button onClick={() => setConfirmingId(action.id)} type="button">拒绝此操作</button>
                  ) : <span>—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Fact({ term, value }: { term: string; value: string }) {
  return <div><dt>{term}</dt><dd>{value}</dd></div>;
}

export function AgentJobDetailPage({
  jobId,
  loadJob = fetchAgentJob,
  cancelJob = cancelAgentJob,
}: {
  jobId: string;
  loadJob?: (jobId: string) => Promise<AgentJobRuntime>;
  cancelJob?: (jobId: string) => Promise<AgentJobCancelResult>;
}) {
  const [resource, setResource] = useState<ResourceState<AgentJobRuntime>>({ kind: "loading" });
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const refresh = useCallback(async () => {
    setResource({ kind: "loading" });
    try {
      setResource({ kind: "ready", value: await loadJob(jobId) });
    } catch {
      setResource({ kind: "error" });
    }
  }, [jobId, loadJob]);

  useEffect(() => { void refresh(); }, [refresh]);

  if (resource.kind === "loading") return <AgentLoading label="Loading Agent Job" />;
  if (resource.kind === "error") return <DetailLoadError label="Agent Job" />;
  const job = resource.value;
  const cancel = async () => {
    setCancelling(true);
    setMutationError(null);
    try {
      await cancelJob(jobId);
      setConfirmCancel(false);
      await refresh();
    } catch (caught) {
      setMutationError(caught instanceof Error ? caught.message : "Agent Job cancellation failed.");
    } finally {
      setCancelling(false);
    }
  };
  const canRequestCancel = !job.authority_ambiguous && !["succeeded", "failed", "cancelled"].includes(job.job_state);
  return (
    <main className="workbench-page" id="main-content">
      <header className="page-heading">
        <p className="eyebrow">Agent Job detail</p>
        <h1><code>{job.job_id}</code></h1>
        <p><a href="/agent">← Agent 工作台</a></p>
      </header>
      {job.authority_ambiguous ? <p className="action-error">Authority is ambiguous; operator commands remain unavailable.</p> : null}
      {mutationError ? <p className="action-error" role="alert">{mutationError}</p> : null}
      <section className="operator-panel" aria-labelledby="agent-operator-actions-heading">
        <div className="panel-heading"><div><h2 id="agent-operator-actions-heading">Operator actions</h2><p>命令只提交请求；后端会在写入时重新校验 Job authority。</p></div><span>backend-authoritative</span></div>
        <div className="record-stack">
          <p>Continuation / resume 暂不开放：当前 FastAPI 尚未接入 durable Agent executor，避免制造“running 但无人执行”的假状态。</p>
          {canRequestCancel ? (confirmCancel ? (
            <div className="button-row"><button disabled={cancelling} onClick={() => void cancel()} type="button">确认取消 Agent Job</button><button disabled={cancelling} onClick={() => setConfirmCancel(false)} type="button">返回</button></div>
          ) : <button onClick={() => setConfirmCancel(true)} type="button">取消 Agent Job</button>) : <p className="field-help">当前 durable lifecycle 不接受取消请求。</p>}
        </div>
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>Lifecycle</h2><strong>{job.job_state}</strong></div>
        <dl className="agent-facts agent-facts--wide">
          <Fact term="Stage" value={job.current_stage ?? "—"} />
          <Fact term="Current run" value={job.current_run_id ?? "—"} />
          <Fact term="Retries" value={String(job.retry_count)} />
          <Fact term="Lease expires" value={job.lease_expires_at ?? "no lease"} />
        </dl>
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>Run history</h2><span>{job.runs.length}</span></div>
        {job.runs.length === 0 ? <p className="panel-empty">No bound runs.</p> : <ul className="compact-list">{job.runs.map((run) => <li key={run.run_id}><a href={`/agent/runs/${encodeURIComponent(run.run_id)}`}><code>{run.run_id}</code></a> — {run.state} — {run.goal}</li>)}</ul>}
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>Evidence</h2><span>{job.evidence_refs.length}</span></div>
        {job.evidence_refs.length === 0 ? <p className="panel-empty">No durable evidence refs.</p> : <ul className="compact-list">{job.evidence_refs.map((ref) => <li key={ref}><code>{ref}</code></li>)}</ul>}
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>Artifacts</h2><span>{job.artifacts.length}</span></div>
        {job.artifacts.length === 0 ? <p className="panel-empty">No durable artifact summaries.</p> : <div className="table-scroll"><table><thead><tr><th>ID</th><th>Kind</th><th>Producer</th><th>Created</th></tr></thead><tbody>{job.artifacts.map((artifact) => <tr key={artifact.id}><th scope="row">{artifact.id}</th><td>{artifact.kind}</td><td>{artifact.producer}</td><td><time dateTime={artifact.created_at}>{artifact.created_at}</time></td></tr>)}</tbody></table></div>}
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>Pending Human Actions</h2><span>{job.pending_human_actions.length}</span></div>
        {job.pending_human_actions.length === 0 ? <p className="panel-empty">No pending HumanAction.</p> : <ul className="compact-list">{job.pending_human_actions.map((action) => <li key={action.id}><code>{action.id}</code> — {action.tool_name} — {action.status}</li>)}</ul>}
      </section>
    </main>
  );
}

export function AgentRunDetailPage({ runId, loadRun = fetchAgentRun }: { runId: string; loadRun?: (runId: string) => Promise<AgentRunDetail> }) {
  const [resource, setResource] = useState<ResourceState<AgentRunDetail>>({ kind: "loading" });

  useEffect(() => {
    let active = true;
    setResource({ kind: "loading" });
    void loadRun(runId).then((value) => { if (active) setResource({ kind: "ready", value }); }).catch(() => { if (active) setResource({ kind: "error" }); });
    return () => { active = false; };
  }, [loadRun, runId]);

  if (resource.kind === "loading") return <AgentLoading label="Loading Agent Run" />;
  if (resource.kind === "error") return <DetailLoadError label="Agent Run" />;
  const run = resource.value;
  return (
    <main className="workbench-page" id="main-content">
      <header className="page-heading">
        <p className="eyebrow">Agent Run detail</p>
        <h1><code>{run.run_id}</code></h1>
        <p><a href="/agent">← Agent 工作台</a> · Job <a href={`/agent/jobs/${encodeURIComponent(run.job_id)}`}><code>{run.job_id}</code></a></p>
      </header>
      <section className="operator-panel">
        <div className="panel-heading"><h2>Run summary</h2><strong>{run.state}</strong></div>
        <div className="record-stack"><p>{run.goal}</p><p className="agent-identity-line">{run.step_count} steps · {run.model_calls} model calls · {run.input_tokens} input tokens · {run.output_tokens} output tokens</p>{run.error_category ? <p className="action-error">{run.error_category}: {run.error_detail ?? "No detail"}</p> : null}</div>
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>Step timeline</h2><span>{run.steps.length}</span></div>
        {run.steps.length === 0 ? <p className="panel-empty">No durable steps.</p> : (
          <ol className="agent-step-list">{run.steps.map((step) => <li key={`${step.step_index}-${step.kind}`}><header><strong>#{step.step_index} · {step.kind}</strong><span>{step.status}</span></header><p>{step.tool_name ? `Tool: ${step.tool_name}` : "No tool"}{step.tool_call_id ? ` · ${step.tool_call_id}` : ""}</p>{step.evidence_refs.length > 0 ? <p>Evidence: {step.evidence_refs.map((ref) => <code key={ref}>{ref} </code>)}</p> : null}{step.error_category ? <p className="action-error">{step.error_category}: {step.error_detail ?? "No detail"}</p> : null}</li>)}</ol>
        )}
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>Human Actions</h2><span>{run.human_actions.length}</span></div>
        {run.human_actions.length === 0 ? <p className="panel-empty">No HumanAction for this run.</p> : <ul className="compact-list">{run.human_actions.map((action) => <li key={action.id}><code>{action.id}</code> — {action.tool_name} — {action.status}</li>)}</ul>}
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>Evidence</h2><span>{run.evidence_refs.length}</span></div>
        {run.evidence_refs.length === 0 ? <p className="panel-empty">No durable evidence refs.</p> : <ul className="compact-list">{run.evidence_refs.map((ref) => <li key={ref}><code>{ref}</code></li>)}</ul>}
      </section>
    </main>
  );
}

function AgentLoading({ label }: { label: string }) {
  return <main className="workbench-page" id="main-content"><section aria-busy="true" aria-label={label} className="loading-panel"><p className="eyebrow">Durable Agent orchestration</p><p role="status">{label}</p><div aria-hidden="true" className="loading-line" /><div aria-hidden="true" className="loading-line loading-line--short" /></section></main>;
}

function AgentLoadError({ onRetry }: { onRetry: () => Promise<void> }) {
  return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>Could not load Agent workbench</h1><p>One or more durable Agent projection endpoints failed.</p><button onClick={() => void onRetry()} type="button">Retry Agent workbench</button></section></main>;
}

function DetailLoadError({ label }: { label: string }) {
  return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>Could not load {label}</h1><p>Return to the Agent workbench and verify the local backend state.</p><a href="/agent">Back to Agent 工作台</a></section></main>;
}
