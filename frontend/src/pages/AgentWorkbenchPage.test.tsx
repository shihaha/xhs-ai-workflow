import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  AgentHumanAction,
  AgentJobRuntime,
  AgentJobSummary,
  AgentOperatorCapabilities,
  AgentRunDetail,
  AgentRunListItem,
  ChatGPTHandoffTask,
} from "../api/client";
import { AgentJobDetailPage, AgentRunDetailPage, AgentWorkbenchPage } from "./AgentWorkbenchPage";

const job: AgentJobSummary = {
  job_id: "job-1",
  job_state: "needs_human",
  current_stage: "manual_chatgpt_required",
  error_category: "manual_chatgpt_required",
  retry_count: 0,
  current_run_id: "run-1",
  authority_ambiguous: false,
  run_count: 1,
  pending_human_action_count: 1,
  evidence_count: 2,
  artifact_count: 1,
  created_at: "2026-08-23T10:00:00Z",
  updated_at: "2026-08-23T10:01:00Z",
};

const run: AgentRunListItem = {
  run_id: "run-1",
  job_id: "job-1",
  goal: "Prepare a durable handoff",
  state: "needs_human",
  model_name: "fake-model",
  prompt_version: "v1",
  step_count: 1,
  model_calls: 1,
  input_tokens: 12,
  output_tokens: 8,
  final_output: null,
  error_category: "manual_chatgpt_required",
  error_detail: "waiting",
  created_at: "2026-08-23T10:00:00Z",
  updated_at: "2026-08-23T10:01:00Z",
  completed_at: null,
};

const action: AgentHumanAction = {
  id: "human-1",
  run_id: "run-1",
  job_id: "job-1",
  tool_call_id: "manual-chatgpt:handoff-1",
  tool_name: "manual_chatgpt",
  status: "pending",
  can_deny: false,
  can_approve: false,
  created_at: "2026-08-23T10:01:00Z",
  resolved_at: null,
};

const continuationUnavailable: AgentOperatorCapabilities = {
  cancel_job: true,
  deny_permission_action: true,
  approve_continuation: false,
  start_grounded_orchestration: false,
  continuation_reason: "Automatic Agent continuation requires a configured app-owned executor/model.",
};

const continuationAvailable: AgentOperatorCapabilities = {
  ...continuationUnavailable,
  approve_continuation: true,
  continuation_reason: null,
};

const orchestrationAvailable: AgentOperatorCapabilities = {
  ...continuationAvailable,
  start_grounded_orchestration: true,
};

const handoff: ChatGPTHandoffTask = {
  handoff_id: "handoff-1",
  job_id: "job-1",
  source_run_id: "run-1",
  human_action_id: "human-1",
  status: "pending",
  human_action_status: "pending",
  job_state: "needs_human",
  current_stage: "manual_chatgpt_required",
  stage_revision: "stage-r1",
  schema_version: "1",
  input_hash: "sha256:abc",
  context_ref_count: 2,
  has_result: false,
  is_current_binding: true,
  authority_ambiguous: false,
  needs_chatgpt: true,
  result_ready: false,
  created_at: "2026-08-23T10:01:00Z",
  accepted_at: null,
};

describe("AgentWorkbenchPage", () => {
  it("renders backend-projected Agent state and ChatGPT attention without mutation controls", async () => {
    render(
      <AgentWorkbenchPage
        loadJobs={async () => [job]}
        loadRuns={async () => [run]}
        loadHumanActions={async () => [action]}
        loadHandoffs={async () => [handoff]}
        loadCapabilities={async () => continuationUnavailable}
      />,
    );

    expect(await screen.findByRole("heading", { name: "Agent 工作台" })).toBeInTheDocument();
    expect(screen.getAllByText("需要 ChatGPT 处理").length).toBeGreaterThan(0);
    for (const link of screen.getAllByRole("link", { name: "job-1" })) expect(link).toHaveAttribute("href", "/agent/jobs/job-1");
    for (const link of screen.getAllByRole("link", { name: "run-1" })) expect(link).toHaveAttribute("href", "/agent/runs/run-1");
    expect(screen.queryByRole("button", { name: /continue|resume|approve|cancel/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/runtime\/external-results/i)).not.toBeInTheDocument();
  });


  it("offers denial only when the backend projection explicitly authorizes it", async () => {
    let deniedActionId: string | null = null;
    render(
      <AgentWorkbenchPage
        loadJobs={async () => [job]}
        loadRuns={async () => [run]}
        loadHumanActions={async () => [{ ...action, can_deny: true }]}
        loadHandoffs={async () => [handoff]}
        loadCapabilities={async () => continuationUnavailable}
        denyAction={async (actionId) => {
          deniedActionId = actionId;
          return {
            human_action_id: actionId,
            job_id: "job-1",
            run_id: "run-1",
            human_action_status: "denied",
            job_state: "failed",
            run_state: "failed",
          };
        }}
      />,
    );

    const denyButton = await screen.findByRole("button", { name: "拒绝此操作" });
    fireEvent.click(denyButton);
    expect(screen.getByRole("button", { name: "确认拒绝并终止" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认拒绝并终止" }));
    await waitFor(() => expect(deniedActionId).toBe("human-1"));
  });

  it("shows accepted handoff as result ready while explicitly leaving continuation to backend", async () => {
    render(
      <AgentWorkbenchPage
        loadJobs={async () => [{ ...job, current_stage: "manual_chatgpt_result_ready", pending_human_action_count: 0 }]}
        loadRuns={async () => [{ ...run, error_category: "manual_chatgpt_result_ready" }]}
        loadHumanActions={async () => [{ ...action, status: "completed", resolved_at: "2026-08-23T10:02:00Z" }]}
        loadHandoffs={async () => [{ ...handoff, status: "accepted", human_action_status: "completed", current_stage: "manual_chatgpt_result_ready", has_result: true, needs_chatgpt: false, result_ready: true, accepted_at: "2026-08-23T10:02:00Z" }]}
        loadCapabilities={async () => continuationUnavailable}
      />,
    );

    expect(await screen.findByText("已交回 / result ready")).toBeInTheDocument();
    expect(screen.getByText(/Job 仍等待后端安全 continuation，不由前端恢复/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resume|continue/i })).not.toBeInTheDocument();
  });

  it("requires backend action authority and executor capability before offering approval", async () => {
    render(
      <AgentWorkbenchPage
        loadJobs={async () => [job]}
        loadRuns={async () => [run]}
        loadHumanActions={async () => [{ ...action, tool_name: "analysis.run_grounded", can_deny: true, can_approve: true }]}
        loadHandoffs={async () => []}
        loadCapabilities={async () => continuationUnavailable}
      />,
    );

    expect(await screen.findByText(/自动 continuation 当前不可用/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批准并继续" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "拒绝此操作" })).toBeInTheDocument();
  });

  it("requires explicit confirmation before approving and continuing", async () => {
    let approvedActionId: string | null = null;
    render(
      <AgentWorkbenchPage
        loadJobs={async () => [job]}
        loadRuns={async () => [run]}
        loadHumanActions={async () => [{ ...action, tool_name: "analysis.run_grounded", can_deny: true, can_approve: true }]}
        loadHandoffs={async () => []}
        loadCapabilities={async () => continuationAvailable}
        approveAction={async (actionId) => {
          approvedActionId = actionId;
          return {
            human_action_id: actionId,
            job_id: "job-1",
            source_run_id: "run-1",
            continuation_run_id: "run-2",
            human_action_status: "approved",
            continuation_enqueued: true,
          };
        }}
      />,
    );

    const approveButton = await screen.findByRole("button", { name: "批准并继续" });
    fireEvent.click(approveButton);
    expect(approvedActionId).toBeNull();
    expect(screen.getByRole("button", { name: "确认批准并继续" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认批准并继续" }));
    await waitFor(() => expect(approvedActionId).toBe("human-1"));
  });

  it("starts grounded orchestration only after evidence selection and explicit confirmation", async () => {
    let startPayload: { goal: string; evidence_ids: string[] } | null = null;
    render(
      <AgentWorkbenchPage
        loadJobs={async () => []}
        loadRuns={async () => []}
        loadHumanActions={async () => []}
        loadHandoffs={async () => []}
        loadCapabilities={async () => orchestrationAvailable}
        loadEvidence={async () => [{ evidence_id: "rank-item:7", kind: "rank_item", account_user_id: "account-a", eligible_for_opportunity: false }]}
        startOrchestration={async (payload) => {
          startPayload = payload;
          return { job_id: "job-new", run_id: "run-new", evidence_count: 1, dispatch_enqueued: true };
        }}
      />,
    );

    expect(await screen.findByRole("heading", { name: "启动 grounded Agent 分析" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("本次目标"), { target: { value: "检查这条证据并决定是否需要分析" } });
    fireEvent.click(screen.getByLabelText(/rank-item:7/));
    fireEvent.click(screen.getByRole("button", { name: "启动 grounded Agent 分析" }));
    expect(startPayload).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "确认启动 Agent" }));
    await waitFor(() => expect(startPayload).toEqual({
      goal: "检查这条证据并决定是否需要分析",
      evidence_ids: ["rank-item:7"],
    }));
  });
});

describe("Agent detail pages", () => {
  it("renders durable evidence and redacted artifact summaries for a Job", async () => {
    const detail: AgentJobRuntime = {
      job_id: job.job_id,
      job_state: job.job_state,
      current_stage: job.current_stage,
      error_category: job.error_category,
      retry_count: 0,
      lease_expires_at: null,
      created_at: job.created_at,
      updated_at: job.updated_at,
      current_run_id: run.run_id,
      authority_ambiguous: false,
      runs: [run],
      pending_human_actions: [action],
      evidence_refs: ["evidence:one", "evidence:two"],
      artifacts: [{ id: 7, job_id: "job-1", kind: "result", producer: "agent-runtime", created_at: "2026-08-23T10:01:00Z" }],
    };

    render(<AgentJobDetailPage jobId="job-1" loadJob={async () => detail} />);

    expect(await screen.findByRole("heading", { name: "Evidence" })).toBeInTheDocument();
    expect(screen.getByText("evidence:one")).toBeInTheDocument();
    expect(screen.getByText("agent-runtime")).toBeInTheDocument();
    expect(screen.queryByText(/C:\\|runtime\/|metadata/i)).not.toBeInTheDocument();
  });


  it("requires a second explicit confirmation before requesting Job cancellation", async () => {
    const detail: AgentJobRuntime = {
      job_id: job.job_id,
      job_state: job.job_state,
      current_stage: job.current_stage,
      error_category: job.error_category,
      retry_count: 0,
      lease_expires_at: null,
      created_at: job.created_at,
      updated_at: job.updated_at,
      current_run_id: run.run_id,
      authority_ambiguous: false,
      runs: [run],
      pending_human_actions: [{ ...action, can_deny: false }],
      evidence_refs: [],
      artifacts: [],
    };
    let cancelCalls = 0;
    render(
      <AgentJobDetailPage
        jobId="job-1"
        loadJob={async () => detail}
        cancelJob={async () => {
          cancelCalls += 1;
          return {
            job_id: "job-1",
            job_state: "cancelled",
            cancelled_run_ids: ["run-1"],
            resolved_human_action_ids: ["human-1"],
            already_cancelled: false,
          };
        }}
      />,
    );

    const requestButton = await screen.findByRole("button", { name: "取消 Agent Job" });
    fireEvent.click(requestButton);
    expect(cancelCalls).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "确认取消 Agent Job" }));
    await waitFor(() => expect(cancelCalls).toBe(1));
  });

  it("renders the durable step timeline without raw tool input/output", async () => {
    const detail: AgentRunDetail = {
      ...run,
      steps: [{
        step_index: 0,
        kind: "tool",
        tool_name: "job.read",
        tool_call_id: "call-1",
        status: "succeeded",
        evidence_refs: ["evidence:one"],
        error_category: null,
        error_detail: null,
        created_at: "2026-08-23T10:00:00Z",
        updated_at: "2026-08-23T10:00:01Z",
      }],
      human_actions: [action],
      evidence_refs: ["evidence:one"],
    };

    render(<AgentRunDetailPage runId="run-1" loadRun={async () => detail} />);

    expect(await screen.findByRole("heading", { name: "Step timeline" })).toBeInTheDocument();
    expect(screen.getByText(/Tool: job.read/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("evidence:one").length).toBeGreaterThan(0));
    expect(screen.queryByText(/input_json|output_json|provider prompt/i)).not.toBeInTheDocument();
  });
});
