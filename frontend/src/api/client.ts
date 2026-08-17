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

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new ApiError(`Request failed with status ${response.status}.`, response.status);
  }
  return response.json() as Promise<T>;
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/api/v1/health");
}

export function fetchJobs(): Promise<JobListResponse> {
  return getJson<JobListResponse>("/api/v1/jobs");
}
