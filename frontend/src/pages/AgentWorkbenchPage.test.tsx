import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  AgentHumanAction,
  AgentJobRuntime,
  AgentJobSummary,
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
  created_at: "2026-08-23T10:01:00Z",
  resolved_at: null,
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
      />,
    );

    expect(await screen.findByRole("heading", { name: "Agent 工作台" })).toBeInTheDocument();
    expect(screen.getAllByText("需要 ChatGPT 处理").length).toBeGreaterThan(0);
    for (const link of screen.getAllByRole("link", { name: "job-1" })) expect(link).toHaveAttribute("href", "/agent/jobs/job-1");
    for (const link of screen.getAllByRole("link", { name: "run-1" })) expect(link).toHaveAttribute("href", "/agent/runs/run-1");
    expect(screen.queryByRole("button", { name: /continue|resume|approve|cancel/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/runtime\/external-results/i)).not.toBeInTheDocument();
  });

  it("shows accepted handoff as result ready while explicitly leaving continuation to backend", async () => {
    render(
      <AgentWorkbenchPage
        loadJobs={async () => [{ ...job, current_stage: "manual_chatgpt_result_ready", pending_human_action_count: 0 }]}
        loadRuns={async () => [{ ...run, error_category: "manual_chatgpt_result_ready" }]}
        loadHumanActions={async () => [{ ...action, status: "completed", resolved_at: "2026-08-23T10:02:00Z" }]}
        loadHandoffs={async () => [{ ...handoff, status: "accepted", human_action_status: "completed", current_stage: "manual_chatgpt_result_ready", has_result: true, needs_chatgpt: false, result_ready: true, accepted_at: "2026-08-23T10:02:00Z" }]}
      />,
    );

    expect(await screen.findByText("已交回 / result ready")).toBeInTheDocument();
    expect(screen.getByText(/Job 仍等待后端安全 continuation，不由前端恢复/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resume|continue/i })).not.toBeInTheDocument();
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
