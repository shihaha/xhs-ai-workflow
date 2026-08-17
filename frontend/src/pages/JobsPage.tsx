import { useCallback, useEffect, useState } from "react";

import { fetchJobs, type Job, type JobArtifact, type JobListResponse, type JobState } from "../api/client";

export interface JobsPageProps {
  loadJobs?: () => Promise<JobListResponse>;
}

type ResourceState =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "ready"; value: JobListResponse };

const STATE_LABELS: Record<JobState, string> = {
  queued: "Queued",
  running: "Running",
  needs_human: "Human attention required",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function JobsPage({ loadJobs = fetchJobs }: JobsPageProps) {
  const [resource, setResource] = useState<ResourceState>({ kind: "loading" });

  const refresh = useCallback(async () => {
    setResource({ kind: "loading" });
    try {
      setResource({ kind: "ready", value: await loadJobs() });
    } catch {
      setResource({ kind: "error" });
    }
  }, [loadJobs]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (resource.kind === "loading") return <LoadingJobs />;
  if (resource.kind === "error") return <JobsError onRetry={refresh} />;
  return <JobsView jobs={resource.value} />;
}

export function JobsView({ jobs }: { jobs: JobListResponse }) {
  return (
    <main className="workbench-page" id="main-content">
      <header className="page-heading page-heading--split">
        <div>
          <p className="eyebrow">Durable work</p>
          <h1>Jobs</h1>
          <p>Persisted execution records, state, logs, and attached evidence.</p>
        </div>
        <p className="result-count" role="status">
          {jobs.length} {jobs.length === 1 ? "job" : "jobs"} returned
        </p>
      </header>

      {jobs.length === 0 ? <EmptyJobs /> : <JobList jobs={jobs} />}
    </main>
  );
}

function JobList({ jobs }: { jobs: Job[] }) {
  return (
    <ol aria-label="Persisted jobs" className="job-list">
      {jobs.map((job) => (
        <JobItem job={job} key={job.id} />
      ))}
    </ol>
  );
}

function JobItem({ job }: { job: Job }) {
  const evidenceLabel = `${job.artifacts.length} evidence ${job.artifacts.length === 1 ? "item" : "items"}`;

  return (
    <li className="job-record">
      <header className="job-record__header">
        <div>
          <h2>{job.type}</h2>
          <p className="job-id">{job.id}</p>
        </div>
        <span className={`state state--${job.state}`}>{STATE_LABELS[job.state]}</span>
      </header>

      <dl className="job-facts">
        <div>
          <dt>Progress</dt>
          <dd>
            {job.progress_total === null ? (
              "Progress not reported"
            ) : (
              <>
                {job.progress_total > 0 ? <progress max={job.progress_total} value={job.progress_current} /> : null}
                <span>{job.progress_current} / {job.progress_total} reported</span>
              </>
            )}
          </dd>
        </div>
        <div>
          <dt>Stage</dt>
          <dd>{job.current_stage ? `Current stage: ${job.current_stage}` : "No current stage reported"}</dd>
        </div>
        <div>
          <dt>Retries</dt>
          <dd>Retries: {job.retry_count}</dd>
        </div>
        <div>
          <dt>Last updated</dt>
          <dd><time dateTime={job.updated_at}>{job.updated_at}</time></dd>
        </div>
      </dl>

      {job.error_category ? <p className="job-error">Error category: {job.error_category}</p> : null}

      <div className="job-record__footer">
        <a href={`#job-${job.id}-evidence`}>{evidenceLabel}</a>
        <span>Logs: {job.logs.length}</span>
      </div>

      <JobLogs logs={job.logs} />
      <JobEvidence artifacts={job.artifacts} jobId={job.id} />
    </li>
  );
}

function JobLogs({ logs }: { logs: Job["logs"] }) {
  return (
    <section aria-label="Job logs" className="job-detail">
      <h3>Logs</h3>
      {logs.length === 0 ? (
        <p>No recorded logs.</p>
      ) : (
        <ul>
          {logs.map((log, index) => <li key={`${log.level}-${index}`}><strong>{log.level}</strong>: {log.message}</li>)}
        </ul>
      )}
    </section>
  );
}

function JobEvidence({ artifacts, jobId }: { artifacts: JobArtifact[]; jobId: string }) {
  return (
    <section aria-label="Attached evidence" className="job-detail" id={`job-${jobId}-evidence`}>
      <h3>Evidence</h3>
      {artifacts.length === 0 ? (
        <p>No evidence paths attached.</p>
      ) : (
        <ul>
          {artifacts.map((artifact) => <li key={`${artifact.kind}-${artifact.path}`}><strong>{artifact.kind}</strong>: <code>{artifact.path}</code></li>)}
        </ul>
      )}
    </section>
  );
}

function EmptyJobs() {
  return (
    <section className="message-panel" role="status">
      <h2>No jobs recorded</h2>
      <p>The API returned no persisted jobs. New work will appear here only after it is created.</p>
    </section>
  );
}

function LoadingJobs() {
  return (
    <main className="workbench-page" id="main-content">
      <section aria-busy="true" aria-label="Loading jobs" className="loading-panel">
        <p className="eyebrow">Durable work</p>
        <p role="status">Loading jobs</p>
        <div aria-hidden="true" className="loading-line" />
        <div aria-hidden="true" className="loading-line loading-line--short" />
      </section>
    </main>
  );
}

function JobsError({ onRetry }: { onRetry: () => Promise<void> }) {
  return (
    <main className="workbench-page" id="main-content">
      <section className="message-panel" role="alert">
        <h1>Could not load jobs</h1>
        <p>The jobs endpoint did not return a result. Check the local backend, then try again.</p>
        <button onClick={() => void onRetry()} type="button">Retry jobs</button>
      </section>
    </main>
  );
}
