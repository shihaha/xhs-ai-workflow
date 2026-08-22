export interface FinishedProductDossier {
  id: string;
  product_key: string;
  name: string;
  version: string;
  target_user: string;
  core_need: string;
  deliverables: string[];
  usage_instructions: string;
  faq: Array<{ question: string; answer: string }>;
  allowed_claims: string[];
  forbidden_claims: string[];
  source_index: string[];
  uat_status: "passed";
  created_at: string;
}

export type KeywordCategory =
  | "main"
  | "positioning"
  | "visual"
  | "audience"
  | "pain"
  | "scenario"
  | "selling_point"
  | "question"
  | "comparison";

export interface KeywordPlanItem {
  id: string;
  run_id: string;
  position: number;
  keyword: string;
  category: KeywordCategory;
  expand: boolean;
  scope: string;
  target_count: number;
  created_at: string;
}

export interface KeywordPlan {
  dossier_id: string;
  run_id: string | null;
  source: "manual" | "ai" | null;
  provider: string | null;
  model: string | null;
  prompt_version: string | null;
  usage: Record<string, number>;
  duration_ms: number | null;
  created_at: string | null;
  count: number;
  items: KeywordPlanItem[];
}

export type BenchmarkJobState =
  | "queued"
  | "running"
  | "needs_human"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface BenchmarkSearchNote {
  note_id: string;
  source_url: string;
  title: string | null;
  summary: string | null;
  user_id: string | null;
}

export interface BenchmarkSearch {
  id: string;
  dossier_id: string;
  keyword_run_id: string;
  keyword_item_id: string;
  stage: "probe" | "full";
  attempt: number;
  keyword: string;
  expected_count: number;
  xhs_job_id: string;
  job_state: BenchmarkJobState;
  progress_current: number;
  progress_total: number | null;
  error_category: string | null;
  result_status: "pending" | "trusted" | "untrusted";
  succeeded_count: number | null;
  artifact_id: number | null;
  collected_at: string | null;
  items: BenchmarkSearchNote[];
  created_at: string;
}

export interface BenchmarkNote extends BenchmarkSearchNote {
  source_search_ids: string[];
  source_keyword_item_ids: string[];
  source_keywords: string[];
}

export interface BenchmarkOverview {
  dossier_id: string;
  current_keyword_run_id: string | null;
  searches: BenchmarkSearch[];
  unique_full_notes: BenchmarkNote[];
  unique_full_note_count: number;
}

export interface FinishedProductDossierCreate {
  product_key: string;
  name: string;
  version: string;
  target_user: string;
  core_need: string;
  deliverables: string[];
  usage_instructions: string;
  faq: Array<{ question: string; answer: string }>;
  allowed_claims: string[];
  forbidden_claims: string[];
  source_index: string[];
  uat_status: "passed";
}

class ContentResearchApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ContentResearchApiError";
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let detail = `Request failed with status ${response.status}.`;
    try {
      const body = await response.json() as { detail?: string };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Keep the HTTP status as the factual fallback.
    }
    throw new ContentResearchApiError(detail, response.status);
  }
  return response.json() as Promise<T>;
}

export const fetchFinishedProductDossiers = () =>
  requestJson<FinishedProductDossier[]>("/api/v1/content-research/dossiers");

export const createFinishedProductDossier = (payload: FinishedProductDossierCreate) =>
  requestJson<FinishedProductDossier>("/api/v1/content-research/dossiers", {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const fetchKeywordPlan = (dossierId: string) =>
  requestJson<KeywordPlan>(
    `/api/v1/content-research/dossiers/${encodeURIComponent(dossierId)}/keywords`,
  );

export const generateKeywordPlan = (dossierId: string) =>
  requestJson<KeywordPlan>(
    `/api/v1/content-research/dossiers/${encodeURIComponent(dossierId)}/keywords/generate`,
    { method: "POST" },
  );

export const fetchKeywordPlanRuns = (dossierId: string) =>
  requestJson<KeywordPlan[]>(
    `/api/v1/content-research/dossiers/${encodeURIComponent(dossierId)}/keyword-runs`,
  );

export const fetchBenchmarkOverview = (dossierId: string) =>
  requestJson<BenchmarkOverview>(
    `/api/v1/content-research/dossiers/${encodeURIComponent(dossierId)}/benchmarks`,
  );

export const startBenchmarkSearch = (
  dossierId: string,
  keywordItemId: string,
  stage: "probe" | "full",
) => requestJson<BenchmarkSearch>(
  `/api/v1/content-research/dossiers/${encodeURIComponent(dossierId)}/benchmark-searches`,
  {
    method: "POST",
    body: JSON.stringify({ keyword_item_id: keywordItemId, stage }),
  },
);
