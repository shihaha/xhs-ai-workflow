import { useCallback, useEffect, useState } from "react";

import {
  approveAgentHumanAction,
  cancelAgentJob,
  denyAgentHumanAction,
  fetchAgentHumanActions,
  fetchAgentJob,
  fetchAgentJobs,
  fetchAgentOperatorCapabilities,
  fetchAgentRun,
  fetchAgentRuns,
  fetchAnalysisEvidence,
  fetchChatGPTHandoffs,
  startGroundedAgentOrchestration,
  type AgentGroundedOrchestration,
  type AgentHumanAction,
  type AgentHumanActionApproval,
  type AgentHumanActionDecision,
  type AgentJobCancelResult,
  type AgentJobRuntime,
  type AgentJobSummary,
  type AgentOperatorCapabilities,
  type AgentRunDetail,
  type AgentRunListItem,
  type AnalysisEvidence,
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
  loadCapabilities?: () => Promise<AgentOperatorCapabilities>;
  loadEvidence?: () => Promise<AnalysisEvidence[]>;
  startOrchestration?: (payload: { goal: string; evidence_ids: string[] }) => Promise<AgentGroundedOrchestration>;
  approveAction?: (actionId: string, payload: { note?: string }) => Promise<AgentHumanActionApproval>;
  denyAction?: (actionId: string, payload: { note?: string }) => Promise<AgentHumanActionDecision>;
}

interface AgentWorkbenchData {
  jobs: AgentJobSummary[];
  runs: AgentRunListItem[];
  humanActions: AgentHumanAction[];
  handoffs: ChatGPTHandoffTask[];
  capabilities: AgentOperatorCapabilities;
  evidence: AnalysisEvidence[];
}

const STATE_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  needs_human: "需要人工处理",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  pending: "等待处理",
  approved: "已批准",
  denied: "已拒绝",
  completed: "已处理",
  accepted: "已接收",
  started: "已开始",
  reused: "已复用",
  approval_required: "等待批准",
  manual_chatgpt_required: "需要 ChatGPT 处理",
  manual_chatgpt_result_ready: "ChatGPT 结果已交回",
  agent_orchestration: "Agent 正在执行",
};

const TOOL_LABELS: Record<string, string> = {
  "analysis.run_grounded": "基于证据执行分析",
  "job.read": "读取任务状态",
  manual_chatgpt: "交给 ChatGPT 处理",
};

const EVIDENCE_KIND_LABELS: Record<string, string> = {
  rank_item: "榜单证据",
  account_note: "账号笔记证据",
  artifact: "采集产物证据",
  shop_collection_result: "店铺采集证据",
};

function stateLabel(value: string | null | undefined): string {
  if (!value) return "暂无";
  return STATE_LABELS[value] ?? `其他状态（${value}）`;
}

function toolLabel(value: string): string {
  return TOOL_LABELS[value] ?? `技术操作（${value}）`;
}

function evidenceKindLabel(value: string): string {
  return EVIDENCE_KIND_LABELS[value] ?? "其他证据";
}

function stepKindLabel(value: string): string {
  const labels: Record<string, string> = {
    model: "Agent 判断",
    tool: "执行工具",
    checkpoint: "保存进度",
    permission: "权限检查",
    human: "人工处理",
  };
  return labels[value] ?? "执行步骤";
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

export function AgentWorkbenchPage({
  loadJobs = fetchAgentJobs,
  loadRuns = fetchAgentRuns,
  loadHumanActions = fetchAgentHumanActions,
  loadHandoffs = fetchChatGPTHandoffs,
  loadCapabilities = fetchAgentOperatorCapabilities,
  loadEvidence = fetchAnalysisEvidence,
  startOrchestration = startGroundedAgentOrchestration,
  approveAction = approveAgentHumanAction,
  denyAction = denyAgentHumanAction,
}: AgentWorkbenchPageProps) {
  const [resource, setResource] = useState<ResourceState<AgentWorkbenchData>>({ kind: "loading" });

  const refresh = useCallback(async () => {
    setResource({ kind: "loading" });
    try {
      const [jobs, runs, humanActions, handoffs, capabilities] = await Promise.all([
        loadJobs(),
        loadRuns(),
        loadHumanActions(),
        loadHandoffs(),
        loadCapabilities(),
      ]);
      const evidence = capabilities.start_grounded_orchestration ? await loadEvidence() : [];
      setResource({ kind: "ready", value: { jobs, runs, humanActions, handoffs, capabilities, evidence } });
    } catch {
      setResource({ kind: "error" });
    }
  }, [loadCapabilities, loadEvidence, loadHandoffs, loadHumanActions, loadJobs, loadRuns]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (resource.kind === "loading") return <AgentLoading label="正在加载 Agent 工作台" />;
  if (resource.kind === "error") return <AgentLoadError onRetry={refresh} />;
  const approve = async (actionId: string) => {
    await approveAction(actionId, {});
    await refresh();
  };
  const deny = async (actionId: string) => {
    await denyAction(actionId, {});
    await refresh();
  };
  const start = async (goal: string, evidenceIds: string[]) => {
    await startOrchestration({ goal, evidence_ids: evidenceIds });
    await refresh();
  };
  return <AgentWorkbenchView data={resource.value} onApprove={approve} onDeny={deny} onStart={start} />;
}

export function AgentWorkbenchView({ data, onApprove, onDeny, onStart }: { data: AgentWorkbenchData; onApprove?: (actionId: string) => Promise<void>; onDeny?: (actionId: string) => Promise<void>; onStart?: (goal: string, evidenceIds: string[]) => Promise<void> }) {
  const pendingHuman = data.humanActions.filter((item) => item.status === "pending").length;
  const activeChatGPT = data.handoffs.filter((item) => item.needs_chatgpt).length;
  const currentRun = data.runs.find((item) => item.state === "running" || item.state === "needs_human") ?? data.runs[0] ?? null;

  return (
    <main className="workbench-page agent-workbench-page" id="main-content">
      <header className="page-heading agent-workbench-heading">
        <div>
          <p className="eyebrow">AI 任务与证据工作台</p>
          <h1>Agent 工作台</h1>
          <p>在这里启动任务、查看 Agent 正在做什么、处理需要你确认的操作，并核对它使用了哪些证据。</p>
        </div>
        <p className="agent-truth-note">状态以后台真实记录为准</p>
      </header>

      <section aria-label="Agent 工作台概览" className="agent-summary-grid">
        <SummaryCard label="任务" value={data.jobs.length} />
        <SummaryCard label="执行记录" value={data.runs.length} />
        <SummaryCard label="等待你处理" value={pendingHuman} />
        <SummaryCard label="需要 ChatGPT 处理" value={activeChatGPT} />
      </section>

      <div className="agent-workspace-grid">
        <aside className="agent-workspace-rail" aria-label="任务记录">
          <TaskRail jobs={data.jobs} runs={data.runs} />
        </aside>

        <section className="agent-workspace-main" aria-label="Agent 执行区">
          <GroundedAgentStartSection
            enabled={data.capabilities.start_grounded_orchestration}
            evidence={data.evidence}
            onStart={onStart}
          />
          <CurrentRunSection run={currentRun} />
          <HumanActionsSection
            actions={data.humanActions}
            approveEnabled={data.capabilities.approve_continuation}
            continuationReason={data.capabilities.continuation_reason}
            onApprove={onApprove}
            onDeny={onDeny}
          />
        </section>

        <aside className="agent-workspace-rail" aria-label="依据与交接">
          <EvidenceRail evidence={data.evidence} />
          <HandoffSection handoffs={data.handoffs} />
        </aside>
      </div>

      <details className="agent-technical-details">
        <summary>查看技术详情（任务 ID / AgentRun / 原始记录）</summary>
        <p>这里保留开发和排错需要的技术标识；日常使用不需要理解这些字段。</p>
        <JobsSection jobs={data.jobs} />
        <RunsSection runs={data.runs} />
      </details>
    </main>
  );
}

function TaskRail({ jobs, runs }: { jobs: AgentJobSummary[]; runs: AgentRunListItem[] }) {
  const recentJobs = jobs.slice(0, 6);
  return (
    <section className="agent-rail-card" aria-labelledby="agent-task-rail-heading">
      <div className="agent-rail-heading">
        <div><p className="eyebrow">任务</p><h2 id="agent-task-rail-heading">任务记录</h2></div>
        <span>{jobs.length}</span>
      </div>
      {recentJobs.length === 0 ? <p className="panel-empty">还没有 Agent 任务。可以从中间创建第一条任务。</p> : (
        <ol className="agent-task-list">
          {recentJobs.map((job) => {
            const run = runs.find((item) => item.run_id === job.current_run_id);
            return (
              <li key={job.job_id}>
                <a href={`/agent/jobs/${encodeURIComponent(job.job_id)}`}>
                  <strong>{run?.goal || "Agent 分析任务"}</strong>
                  <span className={`agent-state-dot agent-state-dot--${job.job_state}`} aria-hidden="true" />
                  <span>{stateLabel(job.job_state)}</span>
                  <small>{formatTime(job.updated_at)}</small>
                </a>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function CurrentRunSection({ run }: { run: AgentRunListItem | null }) {
  return (
    <section className="agent-focus-card" aria-labelledby="agent-current-run-heading">
      <div className="panel-heading agent-focus-heading">
        <div>
          <p className="eyebrow">当前执行</p>
          <h2 id="agent-current-run-heading">Agent 现在在做什么</h2>
        </div>
        {run ? <span className={`agent-badge agent-badge--${run.state === "needs_human" ? "attention" : "ready"}`}>{stateLabel(run.state)}</span> : null}
      </div>
      {!run ? <p className="panel-empty">当前没有正在执行或等待处理的 Agent 任务。</p> : (
        <div className="agent-current-run">
          <h3>{run.goal}</h3>
          <p>{run.state === "needs_human" ? "Agent 已暂停，正在等待你的确认。" : run.state === "running" ? "Agent 正在根据现有证据继续执行。" : `最近一次执行状态：${stateLabel(run.state)}。`}</p>
          <div className="agent-current-run-metrics">
            <span>已记录 {run.step_count} 步</span>
            <span>模型调用 {run.model_calls} 次</span>
          </div>
          <a className="agent-text-link" href={`/agent/runs/${encodeURIComponent(run.run_id)}`}>查看执行过程 →</a>
        </div>
      )}
    </section>
  );
}

function EvidenceRail({ evidence }: { evidence: AnalysisEvidence[] }) {
  return (
    <section className="agent-rail-card" aria-labelledby="agent-evidence-rail-heading">
      <div className="agent-rail-heading">
        <div><p className="eyebrow">依据</p><h2 id="agent-evidence-rail-heading">可用证据</h2></div>
        <span>{evidence.length}</span>
      </div>
      {evidence.length === 0 ? <p className="panel-empty">当前没有可用于新 Agent 分析的证据。</p> : (
        <ul className="agent-evidence-list">
          {evidence.slice(0, 10).map((item) => (
            <li key={item.evidence_id}>
              <strong>{evidenceKindLabel(item.kind)}</strong>
              <span>{item.account_user_id ? `账号：${item.account_user_id}` : "未绑定账号"}</span>
              <details><summary>查看技术标识</summary><code>{item.evidence_id}</code></details>
            </li>
          ))}
        </ul>
      )}
      {evidence.length > 10 ? <p className="field-help">另外还有 {evidence.length - 10} 条证据，可在创建任务时选择。</p> : null}
    </section>
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

function GroundedAgentStartSection({
  enabled,
  evidence,
  onStart,
}: {
  enabled: boolean;
  evidence: AnalysisEvidence[];
  onStart?: (goal: string, evidenceIds: string[]) => Promise<void>;
}) {
  const [goal, setGoal] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggle = (evidenceId: string) => {
    setConfirming(false);
    setSelected((current) => current.includes(evidenceId)
      ? current.filter((item) => item !== evidenceId)
      : current.length < 20 ? [...current, evidenceId] : current);
  };
  const canSubmit = enabled && Boolean(onStart) && goal.trim().length > 0 && selected.length > 0;
  const submit = async () => {
    if (!onStart || !canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      await onStart(goal.trim(), selected);
      setGoal("");
      setSelected([]);
      setConfirming(false);
    } catch {
      setError("任务启动失败。请刷新证据后重试；如果仍失败，再查看技术详情。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="agent-focus-card agent-start-card" aria-labelledby="agent-start-heading">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">新任务</p>
          <h2 id="agent-start-heading">启动基于证据的 Agent 分析</h2>
          <p>先选择已有证据，再告诉 Agent 你想判断什么。需要真正执行分析时，它会停下来等你确认。</p>
        </div>
        <span>已选 {selected.length}/20</span>
      </div>
      {!enabled ? <p className="field-help">当前还没有配置自动 Agent 模型，所以暂时不能启动新任务。</p> : null}
      {error ? <p className="action-error" role="alert">{error}</p> : null}
      <div className="action-form">
        <label>
          本次目标
          <textarea
            disabled={!enabled || busy}
            maxLength={2000}
            onChange={(event) => { setGoal(event.target.value); setConfirming(false); }}
            placeholder="例如：根据这些已有证据，判断这个方向值不值得继续分析，并说明依据。"
            value={goal}
          />
        </label>
        <fieldset disabled={!enabled || busy}>
          <legend>选择证据（最多 20 条）</legend>
          {evidence.length === 0 ? <p className="panel-empty">当前没有可用于新任务的证据。</p> : evidence.map((item) => (
            <label className="check-label" key={item.evidence_id}>
              <input
                aria-label={`选择${evidenceKindLabel(item.kind)} ${item.evidence_id}`}
                checked={selected.includes(item.evidence_id)}
                disabled={!selected.includes(item.evidence_id) && selected.length >= 20}
                onChange={() => toggle(item.evidence_id)}
                type="checkbox"
              />
              <span><strong>{evidenceKindLabel(item.kind)}</strong>{item.account_user_id ? ` · 账号 ${item.account_user_id}` : " · 未绑定账号"}</span>
            </label>
          ))}
        </fieldset>
        {confirming ? (
          <div className="record-stack">
            <p className="action-notice">确认后会创建一个真实 Agent 任务。Agent 只能在你选中的证据范围内开始判断。</p>
            <div className="button-row">
              <button disabled={busy} onClick={() => void submit()} type="button">确认启动 Agent</button>
              <button disabled={busy} onClick={() => setConfirming(false)} type="button">返回</button>
            </div>
          </div>
        ) : <button disabled={!canSubmit || busy} onClick={() => setConfirming(true)} type="button">启动 Agent 分析</button>}
      </div>
    </section>
  );
}

function HandoffSection({ handoffs }: { handoffs: ChatGPTHandoffTask[] }) {
  return (
    <section className="agent-rail-card" aria-labelledby="agent-handoffs-heading">
      <div className="agent-rail-heading">
        <div>
          <p className="eyebrow">交接</p>
          <h2 id="agent-handoffs-heading">ChatGPT 交接</h2>
        </div>
        <span>{handoffs.length}</span>
      </div>
      {handoffs.length === 0 ? (
        <p className="panel-empty">当前没有需要 ChatGPT 接手处理的任务。</p>
      ) : (
        <ol className="agent-handoff-list">
          {handoffs.map((handoff) => (
            <li key={handoff.handoff_id}>
              <div className="agent-handoff-summary">
                <strong>{handoff.needs_chatgpt ? "等待 ChatGPT 处理" : handoff.result_ready ? "ChatGPT 结果已交回" : stateLabel(handoff.status)}</strong>
                <HandoffBadge handoff={handoff} />
              </div>
              {handoff.authority_ambiguous ? (
                <p className="action-error">后台记录存在冲突，当前不能继续操作。</p>
              ) : handoff.result_ready ? (
                <p className="action-notice">结果已经保存。任务是否继续由后台安全规则决定，页面不会自行恢复。</p>
              ) : handoff.needs_chatgpt ? (
                <p className="inline-status">这条任务需要由 ChatGPT 继续处理。</p>
              ) : null}
              <details>
                <summary>查看技术详情</summary>
                <p className="agent-identity-line">交接 ID <code>{handoff.handoff_id}</code> · 任务 <a href={`/agent/jobs/${encodeURIComponent(handoff.job_id)}`}><code>{handoff.job_id}</code></a></p>
                <dl className="agent-facts agent-facts--compact">
                  <Fact term="交接状态" value={stateLabel(handoff.status)} />
                  <Fact term="人工处理" value={stateLabel(handoff.human_action_status)} />
                  <Fact term="任务状态" value={stateLabel(handoff.job_state)} />
                  <Fact term="上下文引用" value={String(handoff.context_ref_count)} />
                </dl>
              </details>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function HandoffBadge({ handoff }: { handoff: ChatGPTHandoffTask }) {
  if (handoff.authority_ambiguous) return <span className="agent-badge agent-badge--danger">记录冲突</span>;
  if (handoff.needs_chatgpt) return <span className="agent-badge agent-badge--attention">需要 ChatGPT 处理</span>;
  if (handoff.result_ready) return <span className="agent-badge agent-badge--ready">结果已交回</span>;
  return <span className="agent-badge">{stateLabel(handoff.status)}</span>;
}

function JobsSection({ jobs }: { jobs: AgentJobSummary[] }) {
  return (
    <section className="operator-panel" aria-labelledby="agent-jobs-heading">
      <div className="panel-heading">
        <div>
          <h2 id="agent-jobs-heading">任务原始记录</h2>
          <p>这里保留后台任务状态、当前执行记录和计数，主要用于排错。</p>
        </div>
        <span>{jobs.length} 条</span>
      </div>
      {jobs.length === 0 ? <p className="panel-empty">当前没有 Agent 任务记录。</p> : (
        <div className="table-scroll">
          <table>
            <thead><tr><th>任务 ID</th><th>状态 / 阶段</th><th>当前执行记录</th><th>后台记录</th><th>更新时间</th></tr></thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.job_id}>
                  <th scope="row"><a href={`/agent/jobs/${encodeURIComponent(job.job_id)}`}><code>{job.job_id}</code></a>{job.authority_ambiguous ? <small>后台记录冲突</small> : null}</th>
                  <td><strong>{stateLabel(job.job_state)}</strong><br /><span>{stateLabel(job.current_stage)}</span></td>
                  <td>{job.current_run_id ? <a href={`/agent/runs/${encodeURIComponent(job.current_run_id)}`}><code>{job.current_run_id}</code></a> : "—"}</td>
                  <td>{job.run_count} 次执行 · {job.pending_human_action_count} 条待处理 · {job.evidence_count} 条证据 · {job.artifact_count} 个产物</td>
                  <td><time dateTime={job.updated_at}>{formatTime(job.updated_at)}</time></td>
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
      <div className="panel-heading"><div><h2 id="agent-runs-heading">Agent 执行原始记录</h2><p>每一次 Agent 执行都会单独保存，方便追踪和排错。</p></div><span>{runs.length} 条</span></div>
      {runs.length === 0 ? <p className="panel-empty">当前没有 Agent 执行记录。</p> : (
        <ol className="agent-record-list">
          {runs.map((run) => (
            <li className="agent-record agent-record--compact" key={run.run_id}>
              <header>
                <div><h3><a href={`/agent/runs/${encodeURIComponent(run.run_id)}`}><code>{run.run_id}</code></a></h3><p>{run.goal}</p></div>
                <span className="agent-badge">{stateLabel(run.state)}</span>
              </header>
              <p className="agent-identity-line">任务 <a href={`/agent/jobs/${encodeURIComponent(run.job_id)}`}><code>{run.job_id}</code></a> · {run.step_count} 步 · 模型调用 {run.model_calls} 次 · {run.input_tokens + run.output_tokens} tokens</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function HumanActionsSection({
  actions,
  approveEnabled,
  continuationReason,
  onApprove,
  onDeny,
}: {
  actions: AgentHumanAction[];
  approveEnabled: boolean;
  continuationReason: string | null;
  onApprove?: (actionId: string) => Promise<void>;
  onDeny?: (actionId: string) => Promise<void>;
}) {
  const [approveConfirmingId, setApproveConfirmingId] = useState<string | null>(null);
  const [denyConfirmingId, setDenyConfirmingId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const approve = async (actionId: string) => {
    if (!onApprove) return;
    setBusyId(actionId);
    setError(null);
    try {
      await onApprove(actionId);
      setApproveConfirmingId(null);
    } catch {
      setError("批准失败。后台没有接受这次操作，请刷新后再试。");
    } finally {
      setBusyId(null);
    }
  };

  const deny = async (actionId: string) => {
    if (!onDeny) return;
    setBusyId(actionId);
    setError(null);
    try {
      await onDeny(actionId);
      setDenyConfirmingId(null);
    } catch {
      setError("拒绝失败。后台没有接受这次操作，请刷新后再试。");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="agent-focus-card" aria-labelledby="agent-human-heading">
      <div className="panel-heading">
        <div><p className="eyebrow">需要确认</p><h2 id="agent-human-heading">等待你处理</h2><p>Agent 遇到需要明确授权的操作时，会停在这里等你决定。</p></div>
        <span>{actions.filter((item) => item.status === "pending").length}</span>
      </div>
      {!approveEnabled && continuationReason ? <p className="field-help">当前自动继续功能不可用。已经等待中的任务不会被页面擅自恢复。</p> : null}
      {error ? <p className="action-error" role="alert">{error}</p> : null}
      {actions.length === 0 ? <p className="panel-empty">目前没有需要你处理的操作。</p> : (
        <ol className="agent-approval-list">
          {actions.map((action) => {
            const canApprove = action.can_approve && approveEnabled && Boolean(onApprove);
            const canDeny = action.can_deny && Boolean(onDeny);
            return (
              <li key={action.id} className={action.status === "pending" ? "agent-approval-card agent-approval-card--pending" : "agent-approval-card"}>
                <div className="agent-approval-heading">
                  <div><strong>{toolLabel(action.tool_name)}</strong><p>{action.status === "pending" ? "Agent 请求执行这一步，需要你明确确认。" : `这条请求已经${stateLabel(action.status)}。`}</p></div>
                  <span className={`agent-badge ${action.status === "pending" ? "agent-badge--attention" : ""}`}>{stateLabel(action.status)}</span>
                </div>
                <p className="field-help">创建时间：{formatTime(action.created_at)}</p>
                {canApprove || canDeny ? (
                  <div className="button-row">
                    {canApprove ? (approveConfirmingId === action.id ? (
                      <>
                        <button disabled={busyId === action.id} onClick={() => void approve(action.id)} type="button">确认批准并继续</button>
                        <button className="button-secondary" disabled={busyId === action.id} onClick={() => setApproveConfirmingId(null)} type="button">返回</button>
                      </>
                    ) : <button onClick={() => { setDenyConfirmingId(null); setApproveConfirmingId(action.id); }} type="button">批准并继续</button>) : null}
                    {canDeny ? (denyConfirmingId === action.id ? (
                      <>
                        <button className="button-danger" disabled={busyId === action.id} onClick={() => void deny(action.id)} type="button">确认拒绝并终止</button>
                        <button className="button-secondary" disabled={busyId === action.id} onClick={() => setDenyConfirmingId(null)} type="button">返回</button>
                      </>
                    ) : <button className="button-secondary" onClick={() => { setApproveConfirmingId(null); setDenyConfirmingId(action.id); }} type="button">拒绝此操作</button>) : null}
                  </div>
                ) : null}
                <details>
                  <summary>查看技术详情</summary>
                  <p className="agent-identity-line">操作 ID <code>{action.id}</code> · 工具 <code>{action.tool_name}</code></p>
                  <p className="agent-identity-line">任务 <a href={`/agent/jobs/${encodeURIComponent(action.job_id)}`}><code>{action.job_id}</code></a> · 执行记录 <a href={`/agent/runs/${encodeURIComponent(action.run_id)}`}><code>{action.run_id}</code></a></p>
                </details>
              </li>
            );
          })}
        </ol>
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

  if (resource.kind === "loading") return <AgentLoading label="正在加载 Agent 任务" />;
  if (resource.kind === "error") return <DetailLoadError label="Agent 任务" />;
  const job = resource.value;
  const cancel = async () => {
    setCancelling(true);
    setMutationError(null);
    try {
      await cancelJob(jobId);
      setConfirmCancel(false);
      await refresh();
    } catch {
      setMutationError("取消任务失败。后台没有接受这次操作，请刷新后再试。");
    } finally {
      setCancelling(false);
    }
  };
  const canRequestCancel = !job.authority_ambiguous && !["succeeded", "failed", "cancelled"].includes(job.job_state);
  return (
    <main className="workbench-page" id="main-content">
      <header className="page-heading">
        <p className="eyebrow">任务详情</p>
        <h1><code>{job.job_id}</code></h1>
        <p><a href="/agent">← Agent 工作台</a></p>
      </header>
      {job.authority_ambiguous ? <p className="action-error">后台任务记录存在冲突，因此暂时不能执行操作。</p> : null}
      {mutationError ? <p className="action-error" role="alert">{mutationError}</p> : null}
      <section className="operator-panel" aria-labelledby="agent-operator-actions-heading">
        <div className="panel-heading"><div><h2 id="agent-operator-actions-heading">任务操作</h2><p>页面只提交你的请求，后台会再次确认当前任务是否允许执行。</p></div><span>后台校验</span></div>
        <div className="record-stack">
          <p>只有后台明确记录为“等待人工处理”的操作才能继续，页面不会自己猜测恢复权限。</p>
          {canRequestCancel ? (confirmCancel ? (
            <div className="button-row"><button className="button-danger" disabled={cancelling} onClick={() => void cancel()} type="button">确认取消 Agent 任务</button><button className="button-secondary" disabled={cancelling} onClick={() => setConfirmCancel(false)} type="button">返回</button></div>
          ) : <button className="button-secondary" onClick={() => setConfirmCancel(true)} type="button">取消 Agent 任务</button>) : <p className="field-help">当前任务状态不能再取消。</p>}
        </div>
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>任务状态</h2><strong>{stateLabel(job.job_state)}</strong></div>
        <dl className="agent-facts agent-facts--wide">
          <Fact term="当前阶段" value={stateLabel(job.current_stage)} />
          <Fact term="当前执行记录" value={job.current_run_id ?? "暂无"} />
          <Fact term="重试次数" value={String(job.retry_count)} />
          <Fact term="执行租约到期" value={job.lease_expires_at ? formatTime(job.lease_expires_at) : "当前没有执行租约"} />
        </dl>
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>执行记录</h2><span>{job.runs.length}</span></div>
        {job.runs.length === 0 ? <p className="panel-empty">当前没有执行记录。</p> : <ul className="compact-list">{job.runs.map((run) => <li key={run.run_id}><a href={`/agent/runs/${encodeURIComponent(run.run_id)}`}>{run.goal}</a> — {stateLabel(run.state)}</li>)}</ul>}
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>证据</h2><span>{job.evidence_refs.length}</span></div>
        {job.evidence_refs.length === 0 ? <p className="panel-empty">这条任务当前没有保存证据引用。</p> : <ul className="compact-list">{job.evidence_refs.map((ref) => <li key={ref}><code>{ref}</code></li>)}</ul>}
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>产物</h2><span>{job.artifacts.length}</span></div>
        {job.artifacts.length === 0 ? <p className="panel-empty">这条任务当前没有保存产物摘要。</p> : <div className="table-scroll"><table><thead><tr><th>ID</th><th>类型</th><th>产生来源</th><th>创建时间</th></tr></thead><tbody>{job.artifacts.map((artifact) => <tr key={artifact.id}><th scope="row">{artifact.id}</th><td>{artifact.kind}</td><td>{artifact.producer}</td><td><time dateTime={artifact.created_at}>{formatTime(artifact.created_at)}</time></td></tr>)}</tbody></table></div>}
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>等待人工处理</h2><span>{job.pending_human_actions.length}</span></div>
        {job.pending_human_actions.length === 0 ? <p className="panel-empty">当前没有等待你处理的操作。</p> : <ul className="compact-list">{job.pending_human_actions.map((action) => <li key={action.id}>{toolLabel(action.tool_name)} — {stateLabel(action.status)} <details><summary>查看技术标识</summary><code>{action.id}</code></details></li>)}</ul>}
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

  if (resource.kind === "loading") return <AgentLoading label="正在加载 Agent 执行记录" />;
  if (resource.kind === "error") return <DetailLoadError label="Agent 执行记录" />;
  const run = resource.value;
  return (
    <main className="workbench-page" id="main-content">
      <header className="page-heading">
        <p className="eyebrow">Agent 执行详情</p>
        <h1><code>{run.run_id}</code></h1>
        <p><a href="/agent">← Agent 工作台</a> · 所属任务 <a href={`/agent/jobs/${encodeURIComponent(run.job_id)}`}><code>{run.job_id}</code></a></p>
      </header>
      <section className="operator-panel">
        <div className="panel-heading"><h2>执行概览</h2><strong>{stateLabel(run.state)}</strong></div>
        <div className="record-stack"><p>{run.goal}</p><p>已记录 {run.step_count} 步 · 模型调用 {run.model_calls} 次 · 输入 {run.input_tokens} tokens · 输出 {run.output_tokens} tokens</p>{run.error_category ? <div className="action-error">这次执行遇到了问题。<details><summary>查看技术错误</summary><code>{run.error_category}</code>{run.error_detail ? `：${run.error_detail}` : ""}</details></div> : null}</div>
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>执行过程</h2><span>{run.steps.length}</span></div>
        {run.steps.length === 0 ? <p className="panel-empty">当前还没有保存执行步骤。</p> : (
          <ol className="agent-step-list">{run.steps.map((step) => <li key={`${step.step_index}-${step.kind}`}><header><strong>第 {step.step_index} 步 · {stepKindLabel(step.kind)}</strong><span>{stateLabel(step.status)}</span></header><p>{step.tool_name ? `操作：${toolLabel(step.tool_name)}` : "这一步没有调用工具"}</p>{step.evidence_refs.length > 0 ? <p>使用证据：{step.evidence_refs.map((ref) => <code key={ref}>{ref} </code>)}</p> : null}{step.error_category ? <div className="action-error">这一步执行失败。<details><summary>查看技术错误</summary><code>{step.error_category}</code>{step.error_detail ? `：${step.error_detail}` : ""}</details></div> : null}{step.tool_call_id ? <details><summary>查看技术标识</summary><code>{step.tool_call_id}</code></details> : null}</li>)}</ol>
        )}
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>人工处理记录</h2><span>{run.human_actions.length}</span></div>
        {run.human_actions.length === 0 ? <p className="panel-empty">这次执行没有人工处理记录。</p> : <ul className="compact-list">{run.human_actions.map((action) => <li key={action.id}>{toolLabel(action.tool_name)} — {stateLabel(action.status)} <details><summary>查看技术标识</summary><code>{action.id}</code></details></li>)}</ul>}
      </section>
      <section className="operator-panel">
        <div className="panel-heading"><h2>使用证据</h2><span>{run.evidence_refs.length}</span></div>
        {run.evidence_refs.length === 0 ? <p className="panel-empty">这次执行没有保存证据引用。</p> : <ul className="compact-list">{run.evidence_refs.map((ref) => <li key={ref}><code>{ref}</code></li>)}</ul>}
      </section>
    </main>
  );
}

function AgentLoading({ label }: { label: string }) {
  return <main className="workbench-page" id="main-content"><section aria-busy="true" aria-label={label} className="loading-panel"><p className="eyebrow">Agent 工作台</p><p role="status">{label}</p><div aria-hidden="true" className="loading-line" /><div aria-hidden="true" className="loading-line loading-line--short" /></section></main>;
}

function AgentLoadError({ onRetry }: { onRetry: () => Promise<void> }) {
  return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>Agent 工作台加载失败</h1><p>后台状态读取失败。请确认本地服务正常后再重试。</p><button onClick={() => void onRetry()} type="button">重新加载</button></section></main>;
}

function DetailLoadError({ label }: { label: string }) {
  return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>{label}加载失败</h1><p>请返回工作台，并确认本地后台服务状态正常。</p><a href="/agent">返回 Agent 工作台</a></section></main>;
}
