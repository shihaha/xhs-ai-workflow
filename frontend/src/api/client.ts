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
  user_id: string; account_name: string; score: number; evidence: number; credibility: number;
  accessibility: number; fans: number; gmv: string; pay: string; read: string; nday: number; nboard: number;
}
export interface DeviceHealth { status: "available" | "unavailable" | "needs_human"; device_id: string | null; detail: string; raw_evidence: Record<string, unknown>; }
export interface AnalysisEvidence { evidence_id: string; kind: string; account_user_id: string | null; eligible_for_opportunity: boolean; }
export interface CollectionQueued { job_id: string; status: "queued"; }
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
export interface Analysis { id: string; analysis_type: string; account_user_id: string | null; account_user_ids: string[]; status: "succeeded" | "failed" | "needs_human"; evidence_ids: string[]; output?: Record<string, unknown> | null; error_category?: string | null; error_detail?: string | null; provider?: string; model?: string; prompt_version?: string; created_at?: string; }
export interface Opportunity { id: string; analysis_id: string; title: string; status: string; summary: string; evidence_ids: string[]; next_action: string; created_at: string; }
export interface Material { id: string; product_id: string; logical_name: string; version: number; path: string; sha256: string; size_bytes: number; media_type: string; kind: "source" | "output_image"; availability: "available" | "missing" | "corrupt"; created_at: string; }
export interface Product { id: string; name: string; target_user: string; opportunity_id: string; materials: Material[]; created_at?: string; }
export interface ContentRevision { id: string; number: number; title: string; body: string; claims: Array<{ claim: string; evidence_ids: string[] }>; source_evidence_ids: string[]; image_plan: Array<{ page_number: number; material_id: string; role: "cover" | "page"; headline: string; visual_direction: string }>; model_provider: string; model_name: string; prompt_version: string; usage: Record<string, number>; attempts: Array<Record<string, unknown>>; created_at: string; }
export interface ContentItem { id: string; product_id: string; opportunity_id: string; template_key: string; status: "research" | "draft" | "review" | "rejected" | "approved" | "exported"; evidence_ids: string[]; material_ids: string[]; image_material_ids: string[]; cover_material_id: string; research_facts: Array<{ fact: string; evidence_ids: string[] }>; current_revision: ContentRevision | null; revisions: ContentRevision[]; reviews: Array<Record<string, unknown>>; export_availability: "available" | "missing" | "corrupt" | "building" | "failed" | null; created_at: string; updated_at: string; }
export interface ContentPackage { id: string; content_item_id: string; revision_id: string; status: "building" | "ready" | "failed"; availability: "available" | "missing" | "corrupt" | "building" | "failed"; path: string; sha256: string; size_bytes: number; created_at: string; }

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
export const ingestRankSnapshot = (payload: Record<string, unknown>) => postJson<RankSnapshot>("/api/v1/radar/rank-snapshots", payload);
export const startQianfanCollection = (payload: { expected_count_per_scope: number }) => postJson<QianfanCollectionQueued>("/api/v1/radar/qianfan-collections", payload);
export const fetchDevices = () => getJson<DeviceHealth[]>("/api/v1/devices");
export const fetchJob = (jobId: string) => getJson<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
export const startAccountCollection = (userId: string, payload: { expected_note_count: number }) => postJson<CollectionQueued>(`/api/v1/accounts/${encodeURIComponent(userId)}/collections`, payload);
export const fetchAccountProfile = (userId: string) => getJson<AccountProfile>(`/api/v1/accounts/${encodeURIComponent(userId)}/profile`);
export const fetchAccountNotes = (userId: string) => getJson<AccountNote[]>(`/api/v1/accounts/${encodeURIComponent(userId)}/notes`);
export const startNoteSearch = (payload: { keyword: string; expected_count: number }) => postJson<CollectionQueued>("/api/v1/notes/search-collections", payload);
export const fetchNoteSearchResults = (jobId: string) => getJson<NoteSearchResults>(`/api/v1/note-search-results?job_id=${encodeURIComponent(jobId)}`);
export const fetchAnalysisEvidence = (accountId?: string) => getJson<AnalysisEvidence[]>(`/api/v1/analysis-evidence${accountId ? `?account_user_id=${encodeURIComponent(accountId)}` : ""}`);
export const fetchAnalyses = () => getJson<Analysis[]>("/api/v1/analyses");
export const fetchOpportunities = () => getJson<Opportunity[]>("/api/v1/opportunities");
export const fetchProducts = () => getJson<Product[]>("/api/v1/products");
export const fetchContentItems = () => getJson<ContentItem[]>("/api/v1/content-items");
export const fetchContentPackages = () => getJson<ContentPackage[]>("/api/v1/content-packages");
export const queueShopCollection = (payload: Record<string, unknown>) => postJson<{ job_id: string; status: "queued" }>("/api/v1/shop-collections", payload);
export const createAnalysis = (payload: Record<string, unknown>) => postJson<Analysis>("/api/v1/analyses", payload);
export const createProduct = (payload: Record<string, unknown>) => postJson<Product>("/api/v1/products", payload);
export const addProductMaterial = (productId: string, payload: Record<string, unknown>) => postJson<Material>(`/api/v1/products/${encodeURIComponent(productId)}/materials`, payload);
export const createContentItem = (payload: Record<string, unknown>) => postJson<ContentItem>("/api/v1/content-items", payload);
export const reviewContentItem = (itemId: string, payload: Record<string, unknown>) => postJson<ContentItem>(`/api/v1/content-items/${encodeURIComponent(itemId)}/reviews`, payload);
export const regenerateContentItem = (itemId: string, payload: Record<string, unknown>) => postJson<ContentItem>(`/api/v1/content-items/${encodeURIComponent(itemId)}/regenerate`, payload);
export const exportContentItem = (itemId: string, payload: Record<string, unknown>) => postJson<ContentPackage>(`/api/v1/content-items/${encodeURIComponent(itemId)}/export`, payload);
