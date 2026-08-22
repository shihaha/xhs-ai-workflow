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
  dossier_id: string;
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
  count: number;
  items: KeywordPlanItem[];
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
