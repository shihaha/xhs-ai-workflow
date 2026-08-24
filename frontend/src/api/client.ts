export type JobState =
  | "queued"
  | "running"
  | "needs_human"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface HealthCheck {
  healthy: boolean;
  path?: string;
  executable?: string | null;
}

export interface HealthResponse {
  status: "healthy" | "degraded";
  checks: Record<string, HealthCheck>;
}

export interface JobLog {
  level: string;
  message: string;
}

export interface JobArtifact {
  kind: string;
  producer?: string;
  path: string;
  metadata: Record<string, unknown>;
}

export interface Job {
  id: string;
  type: string;
  input: Record<string, unknown>;
  state: JobState;
  progress_current: number;
  progress_total: number | null;
  current_stage: string | null;
  error_category: string | null;
  retry_count: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  lease_expires_at: string | null;
  logs: JobLog[];
  artifacts: JobArtifact[];
}

export type JobListResponse = Job[];

export interface AgentRunSummary {
  run_id: string;
  goal: string;
  state: string;
  model_name: string | null;
  prompt_version: string | null;
  step_count: number;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  final_output: Record<string, unknown> | null;
  error_category: string | null;
  error_detail: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface AgentRunListItem extends AgentRunSummary {
  job_id: string;
}

export interface AgentStepSummary {
  step_index: number;
  kind: string;
  tool_name: string | null;
  tool_call_id: string | null;
  status: string;
  evidence_refs: string[];
  error_category: string | null;
  error_detail: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentHumanAction {
  id: string;
  run_id: string;
  job_id: string;
  tool_call_id: string;
  tool_name: string;
  status: string;
  approval_summary: string | null;
  external_side_effect: boolean;
  can_deny: boolean;
  can_approve: boolean;
  created_at: string;
  resolved_at: string | null;
}

export interface AgentHumanActionSummary {
  id: string;
  run_id: string;
  tool_call_id: string;
  tool_name: string;
  status: string;
  approval_summary: string | null;
  external_side_effect: boolean;
  can_deny: boolean;
  can_approve: boolean;
  created_at: string;
  resolved_at: string | null;
}

export interface AgentJobArtifactSummary {
  id: number;
  job_id: string;
  kind: string;
  producer: string;
  created_at: string;
}

export interface AgentJobSummary {
  job_id: string;
  job_state: string;
  current_stage: string | null;
  error_category: string | null;
  retry_count: number;
  current_run_id: string | null;
  authority_ambiguous: boolean;
  run_count: number;
  pending_human_action_count: number;
  evidence_count: number;
  artifact_count: number;
  created_at: string;
  updated_at: string;
}

export interface AgentJobRuntime {
  job_id: string;
  job_state: string;
  current_stage: string | null;
  error_category: string | null;
  retry_count: number;
  lease_expires_at: string | null;
  created_at: string;
  updated_at: string;
  current_run_id: string | null;
  authority_ambiguous: boolean;
  runs: AgentRunSummary[];
  pending_human_actions: AgentHumanActionSummary[];
  evidence_refs: string[];
  artifacts: AgentJobArtifactSummary[];
}

export interface AgentRunDetail extends AgentRunSummary {
  job_id: string;
  steps: AgentStepSummary[];
  human_actions: AgentHumanActionSummary[];
  evidence_refs: string[];
}

export interface ChatGPTHandoffTask {
  handoff_id: string;
  job_id: string;
  source_run_id: string;
  human_action_id: string;
  status: string;
  human_action_status: string;
  job_state: string;
  current_stage: string | null;
  stage_revision: string;
  schema_version: string;
  input_hash: string;
  context_ref_count: number;
  has_result: boolean;
  is_current_binding: boolean;
  authority_ambiguous: boolean;
  needs_chatgpt: boolean;
  result_ready: boolean;
  created_at: string;
  accepted_at: string | null;
}

export interface AgentOperatorCapabilities {
  cancel_job: boolean;
  deny_permission_action: boolean;
  approve_continuation: boolean;
  approve_physical_continuation: boolean;
  start_grounded_orchestration: boolean;
  continuation_reason: string | null;
}

export interface AgentGroundedOrchestration {
  job_id: string;
  run_id: string;
  evidence_count: number;
  dispatch_enqueued: boolean;
  handoff_created: boolean;
  handoff_id: string | null;
}

export interface AgentJobCancelResult {
  job_id: string;
  job_state: "cancelled";
  cancelled_run_ids: string[];
  resolved_human_action_ids: string[];
  already_cancelled: boolean;
}

export interface AgentHumanActionDecision {
  human_action_id: string;
  job_id: string;
  run_id: string;
  human_action_status: "denied";
  job_state: "failed";
  run_state: "failed";
}

export interface AgentHumanActionApproval {
  human_action_id: string;
  job_id: string;
  source_run_id: string;
  continuation_run_id: string;
  human_action_status: "approved";
  continuation_enqueued: true;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
  });
  if (!response.ok) {
    let detail = `Request failed with status ${response.status}.`;
    try {
      const body = await response.json() as { detail?: string };
      if (typeof body.detail === "string") detail = body.detail;
    } catch { /* The HTTP status remains the factual fallback. */ }
    throw new ApiError(detail, response.status);
  }
  return response.json() as Promise<T>;
}

const getJson = <T,>(path: string) => requestJson<T>(path);
const postJson = <T,>(path: string, payload: unknown) => requestJson<T>(path, { method: "POST", body: JSON.stringify(payload) });

export function fetchHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/api/v1/health");
}

export function fetchJobs(): Promise<JobListResponse> {
  return getJson<JobListResponse>("/api/v1/jobs");
}

export interface RankSnapshot {
  id: number; source_date: string; collected_at: string; board: string; dimension: string;
  source_url: string; raw_evidence: Record<string, unknown>; submitted_count: number;
  deduplicated_count: number; items: Array<Record<string, unknown>>;
}
export interface QianfanScopeQueued { job_id: string; board: string; dimension: string; status: "queued"; }
export interface QianfanCollectionQueued { collection_id: string; scopes: QianfanScopeQueued[]; }
export interface Account {
  user_id: string; account_name: string; score: number | null; score_status: "scored" | "insufficient_metrics";
  ranking_evidence_count: number; best_rank: number; evidence: number; credibility: number;
  accessibility: number; fans: number; gmv: string; pay: string; read: string; nday: number; nboard: number;
}
export interface DeviceHealth { status: "available" | "unavailable" | "needs_human"; device_id: string | null; detail: string; raw_evidence: Record<string, unknown>; }
export interface AnalysisEvidence { evidence_id: string; kind: string; account_user_id: string | null; source_date?: string | null; eligible_for_opportunity: boolean; }
export interface CollectionQueued { job_id: string; status: "queued"; }
export interface ShopPreflightCreate {
  account_user_id: string;
  account_name: string;
  collection_mode: "preflight";
  device_id?: string;
}
export interface CandidateFunnel extends Account {
  candidate_position: number;
  prescreen_classification: "pending" | "likely_digital" | "clearly_physical" | "uncertain";
  prescreen_reason: string | null;
  prescreen_evidence_ids: string[];
  prescreened_at: string | null;
  android_scope_classification: "unknown" | "in_scope" | "out_of_scope_physical" | "needs_human";
  android_job_state: "none" | JobState;
  status: "pending_prescreen" | "clearly_physical_skipped" | "likely_digital_waiting_preflight" | "uncertain_waiting_preflight" | "preflight_active" | "in_scope" | "out_of_scope_physical" | "needs_human" | "collection_failed";
}
export interface CandidateAdvanceQueued { account_user_id: string; candidate_position: number; job_id: string; status: "queued"; }
export interface AccountProfile {
  user_id: string; source_url: string; nickname: string | null; bio: string | null;
  public_stats: Record<string, number>; collection_job_id: string; collection_artifact_id: number; collected_at: string;
}
export interface AccountNote {
  note_id: string; user_id: string; source_url: string; title: string | null; summary: string | null;
  published_at: string | null; public_interactions: Record<string, number>; collection_job_id: string;
  collection_artifact_id: number; collected_at: string;
}
export interface SearchNote {
  note_id: string; source_url: string; title: string | null; summary: string | null; user_id: string | null;
}
export interface NoteSearchResults {
  job_id: string; keyword: string; expected_count: number; succeeded_count: number;
  artifact_id: number; collected_at: string; items: SearchNote[];
}
export interface AccountDemandProfile { account_user_id: string; primary_offering: string; target_user: string; core_purchase_motivation: string; delivery_format: string; usage_scenarios: string[]; evidence_ids: string[]; }
export interface CrossAccountDemandConclusion { has_specific_shared_demand: boolean; common_demand: string | null; commonalities: string[]; key_differences: string[]; rationale: string; evidence_ids: string[]; }
export interface AnalysisOutput { account_demand_profiles?: AccountDemandProfile[]; cross_account_conclusion?: CrossAccountDemandConclusion | null; opportunities?: Array<Record<string, unknown>>; [key: string]: unknown; }
export interface Analysis { id: string; analysis_type: string; account_user_id: string | null; account_user_ids: string[]; status: "succeeded" | "failed" | "needs_human"; evidence_ids: string[]; output?: AnalysisOutput | null; error_category?: string | null; error_detail?: string | null; provider?: string; model?: string; prompt_version?: string; created_at?: string; }
export interface OpportunityAccountSupport { account_user_id: string; shop_evidence_ids: string[]; note_evidence_ids: string[]; }
export interface SupportingProduct { account_user_id: string; evidence_id: string; product_id: string; title: string | null; source_url: string; image_evidence_count: number; }
export interface SupportingNote { account_user_id: string; evidence_id: string; note_id: string; title: string | null; source_url: string; }
export interface Opportunity {
  id: string; analysis_id: string; title: string; status: string; summary: string; evidence_ids: string[];
  review_status: "pending_review" | "approved" | "rejected";
  evidence_level: "warming_candidate" | "validated_candidate" | "legacy_ungraded";
  supporting_account_count: number; supporting_accounts: OpportunityAccountSupport[];
  supporting_products: SupportingProduct[]; supporting_notes: SupportingNote[];
  reviewed_at: string | null; rejection_reason: string | null; next_action: string; created_at: string;
}
export interface Material { id: string; product_id: string; logical_name: string; version: number; path: string; sha256: string; size_bytes: number; media_type: string; kind: "source" | "output_image"; availability: "available" | "missing" | "corrupt"; created_at: string; }
export interface Product { id: string; name: string; target_user: string; opportunity_id: string; materials: Material[]; created_at?: string; }
export interface ContentRevision { id: string; number: number; title: string; body: string; claims: Array<{ claim: string; evidence_ids: string[] }>; source_evidence_ids: string[]; image_plan: Array<{ page_number: number; material_id: string; role: "cover" | "page"; headline: string; visual_direction: string }>; model_provider: string; model_name: string; prompt_version: string; usage: Record<string, number>; attempts: Array<Record<string, unknown>>; created_at: string; }
export interface ContentItem { id: string; product_id: string; opportunity_id: string; template_key: string; status: "research" | "draft" | "review" | "rejected" | "approved" | "exported"; evidence_ids: string[]; material_ids: string[]; image_material_ids: string[]; cover_material_id: string; research_facts: Array<{ fact: string; evidence_ids: string[] }>; current_revision: ContentRevision | null; revisions: ContentRevision[]; reviews: Array<Record<string, unknown>>; export_availability: "available" | "missing" | "corrupt" | "building" | "failed" | null; created_at: string; updated_at: string; }
export interface ContentPackage { id: string; content_item_id: string; revision_id: string; status: "building" | "ready" | "failed"; availability: "available" | "missing" | "corrupt" | "building" | "failed"; path: string; sha256: string; size_bytes: number; created_at: string; }
export interface ContentMediaRun {
  id: string; job_id: string; owner_product_id: string; content_item_id: string; revision_id: string;
  plan_entry_id: string | null; capability: "generate" | "analyze"; status: JobState; state_version: number;
  provider: string; model: string; prompt_version: string; input_digest: string;
  allowed_evidence_ids: string[]; allowed_material_ids: string[]; output_material_id: string | null;
  analysis_artifact_id: number | null; usage: Record<string, number>; duration_ms: number | null;
  attempts: Array<{ attempt: number; category: string }>; error_category: string | null; error_detail: string | null;
  lease_token: string | null; lease_expires_at: string | null; created_at: string; updated_at: string; completed_at: string | null;
}
export interface VisualAssessment {
  summary: string; plan_match: boolean; text_readability: string; defects: string[];
  safety_issues: string[]; suggestions: string[];
}
export interface ContentMediaAssessment {
  run_id: string; content_item_id: string; revision_id: string; material_ids: string[];
  provider: string; model: string; prompt_version: string; provider_request_id: string | null;
  assessment: VisualAssessment; usage: Record<string, number>; duration_ms: number;
}

async function getAllPages<T>(path: string): Promise<T[]> {
  const pageSize = 100;
  const rows: T[] = [];
  for (let offset = 0; ; offset += pageSize) {
    const separator = path.includes("?") ? "&" : "?";
    const page = await getJson<T[]>(`${path}${separator}limit=${pageSize}&offset=${offset}`);
    rows.push(...page);
    if (page.length < pageSize) return rows;
  }
}

export const fetchRankSnapshots = () => getAllPages<RankSnapshot>("/api/v1/radar/rank-snapshots");
export const fetchAccounts = () => getAllPages<Account>("/api/v1/radar/accounts");
export const fetchCandidateFunnel = (sourceDate: string) => getJson<CandidateFunnel[]>(`/api/v1/radar/candidate-funnel?source_date=${encodeURIComponent(sourceDate)}&limit=1000`);
export const runCandidatePrescreen = (payload: { source_date: string; limit: number }) => postJson<CandidateFunnel[]>("/api/v1/radar/candidate-prescreens", payload);
export const advanceCandidateFunnel = (payload: { source_date: string; device_id?: string }) => postJson<CandidateAdvanceQueued>("/api/v1/radar/candidate-funnel/advance", payload);
export const ingestRankSnapshot = (payload: Record<string, unknown>) => postJson<RankSnapshot>("/api/v1/radar/rank-snapshots", payload);
export const startQianfanCollection = (payload: { expected_count_per_scope: number }) => postJson<QianfanCollectionQueued>("/api/v1/radar/qianfan-collections", payload);
export const fetchDevices = () => getJson<DeviceHealth[]>("/api/v1/devices");
export const fetchJob = (jobId: string) => getJson<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
export const startAccountCollection = (userId: string, payload: { sample_limit: 10 }) => postJson<CollectionQueued>(`/api/v1/accounts/${encodeURIComponent(userId)}/collections`, payload);
export const fetchAccountProfile = (userId: string) => getJson<AccountProfile>(`/api/v1/accounts/${encodeURIComponent(userId)}/profile`);
export const fetchAccountNotes = (userId: string) => getJson<AccountNote[]>(`/api/v1/accounts/${encodeURIComponent(userId)}/notes`);
export const startNoteSearch = (payload: { keyword: string; expected_count: number }) => postJson<CollectionQueued>("/api/v1/notes/search-collections", payload);
export const fetchNoteSearchResults = (jobId: string) => getJson<NoteSearchResults>(`/api/v1/note-search-results?job_id=${encodeURIComponent(jobId)}`);
export const fetchAnalysisEvidence = (accountId?: string) => getJson<AnalysisEvidence[]>(`/api/v1/analysis-evidence${accountId ? `?account_user_id=${encodeURIComponent(accountId)}` : ""}`);
export const fetchAnalyses = () => getJson<Analysis[]>("/api/v1/analyses");
export const fetchOpportunities = () => getJson<Opportunity[]>("/api/v1/opportunities");
export const reviewOpportunity = (opportunityId: string, payload: { decision: "approve" | "reject"; reason?: string }) => postJson<Opportunity>(`/api/v1/opportunities/${encodeURIComponent(opportunityId)}/review`, payload);
export const fetchProducts = () => getJson<Product[]>("/api/v1/products");
export const fetchContentItems = () => getJson<ContentItem[]>("/api/v1/content-items");
export const fetchContentPackages = () => getJson<ContentPackage[]>("/api/v1/content-packages");
export const queueShopCollection = (payload: ShopPreflightCreate) => postJson<{ job_id: string; status: "queued" }>("/api/v1/shop-collections", payload);
export const createAnalysis = (payload: Record<string, unknown>) => postJson<Analysis>("/api/v1/analyses", payload);
export const createProduct = (payload: Record<string, unknown>) => postJson<Product>("/api/v1/products", payload);
export const addProductMaterial = (productId: string, payload: Record<string, unknown>) => postJson<Material>(`/api/v1/products/${encodeURIComponent(productId)}/materials`, payload);
export const createContentItem = (payload: Record<string, unknown>) => postJson<ContentItem>("/api/v1/content-items", payload);
export const reviewContentItem = (itemId: string, payload: Record<string, unknown>) => postJson<ContentItem>(`/api/v1/content-items/${encodeURIComponent(itemId)}/reviews`, payload);
export const regenerateContentItem = (itemId: string, payload: Record<string, unknown>) => postJson<ContentItem>(`/api/v1/content-items/${encodeURIComponent(itemId)}/regenerate`, payload);
export const exportContentItem = (itemId: string, payload: Record<string, unknown>) => postJson<ContentPackage>(`/api/v1/content-items/${encodeURIComponent(itemId)}/export`, payload);
export const startContentImageGeneration = (itemId: string, payload: { expected_revision_id: string; image_plan_entry_id: string }) => postJson<ContentMediaRun>(`/api/v1/content-items/${encodeURIComponent(itemId)}/image-generations`, payload);
export const startContentImageAnalysis = (itemId: string, payload: { expected_revision_id: string; material_ids: string[] }) => postJson<ContentMediaRun>(`/api/v1/content-items/${encodeURIComponent(itemId)}/image-analyses`, payload);
export const fetchContentMediaRuns = (itemId: string) => getJson<ContentMediaRun[]>(`/api/v1/content-items/${encodeURIComponent(itemId)}/media-runs`);
export const fetchContentMediaRun = (runId: string) => getJson<ContentMediaRun>(`/api/v1/content-media-runs/${encodeURIComponent(runId)}`);
export const fetchContentMediaAssessment = (runId: string) => getJson<ContentMediaAssessment>(`/api/v1/content-media-runs/${encodeURIComponent(runId)}/assessment`);

export const fetchAgentJobs = () => getJson<AgentJobSummary[]>("/api/v1/agent-runtime/jobs");
export const fetchAgentJob = (jobId: string) => getJson<AgentJobRuntime>(`/api/v1/agent-runtime/jobs/${encodeURIComponent(jobId)}`);
export const fetchAgentRuns = () => getJson<AgentRunListItem[]>("/api/v1/agent-runtime/runs");
export const fetchAgentRun = (runId: string) => getJson<AgentRunDetail>(`/api/v1/agent-runtime/runs/${encodeURIComponent(runId)}`);
export const fetchAgentHumanActions = (status?: "pending" | "approved" | "denied" | "completed") => getJson<AgentHumanAction[]>(`/api/v1/agent-runtime/human-actions${status ? `?status=${encodeURIComponent(status)}` : ""}`);
export const fetchAgentOperatorCapabilities = () => getJson<AgentOperatorCapabilities>("/api/v1/agent-runtime/operator-capabilities");
export const startGroundedAgentOrchestration = (payload: { goal: string; evidence_ids: string[] }) => postJson<AgentGroundedOrchestration>("/api/v1/agent-runtime/jobs", payload);
export const cancelAgentJob = (jobId: string) => postJson<AgentJobCancelResult>(`/api/v1/agent-runtime/jobs/${encodeURIComponent(jobId)}/cancel`, {});
export const denyAgentHumanAction = (actionId: string, payload: { note?: string }) => postJson<AgentHumanActionDecision>(`/api/v1/agent-runtime/human-actions/${encodeURIComponent(actionId)}/deny`, payload);
export const approveAgentHumanAction = (actionId: string, payload: { note?: string }) => postJson<AgentHumanActionApproval>(`/api/v1/agent-runtime/human-actions/${encodeURIComponent(actionId)}/approve`, payload);
export const fetchChatGPTHandoffs = (status?: "pending" | "accepted") => getJson<ChatGPTHandoffTask[]>(`/api/v1/agent-runtime/chatgpt-handoffs${status ? `?status=${encodeURIComponent(status)}` : ""}`);
