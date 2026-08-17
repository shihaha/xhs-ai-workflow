import { useCallback, useEffect, useState } from "react";

import { fetchHealth, type HealthCheck, type HealthResponse } from "../api/client";

export interface SystemStatusPageProps {
  loadHealth?: () => Promise<HealthResponse>;
}

type ResourceState =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "ready"; value: HealthResponse };

export function SystemStatusPage({ loadHealth = fetchHealth }: SystemStatusPageProps) {
  const [resource, setResource] = useState<ResourceState>({ kind: "loading" });

  const refresh = useCallback(async () => {
    setResource({ kind: "loading" });
    try {
      setResource({ kind: "ready", value: await loadHealth() });
    } catch {
      setResource({ kind: "error" });
    }
  }, [loadHealth]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (resource.kind === "loading") return <LoadingSystemChecks />;
  if (resource.kind === "error") return <SystemStatusError onRetry={refresh} />;
  return <SystemStatusView health={resource.value} />;
}

export function SystemStatusView({ health }: { health: HealthResponse }) {
  const checks = Object.entries(health.checks);
  const overallLabel = health.status === "healthy" ? "Healthy" : "Degraded";

  return (
    <main className="workbench-page" id="main-content">
      <header className="page-heading">
        <p className="eyebrow">Local prerequisites</p>
        <h1>System status</h1>
        <p>Live checks reported by the local API. No external integration is probed here.</p>
      </header>

      <section aria-labelledby="overall-status-heading" className="status-summary">
        <h2 id="overall-status-heading">Overall status</h2>
        <p className={`state state--${health.status}`} role="status">
          {overallLabel}
        </p>
      </section>

      <section aria-labelledby="checks-heading" className="operator-panel">
        <div className="panel-heading">
          <h2 id="checks-heading">Reported checks</h2>
          <p>{checks.length} checks returned</p>
        </div>
        <dl className="check-list">
          {checks.map(([name, check]) => (
            <HealthCheckRow check={check} key={name} name={name} />
          ))}
        </dl>
      </section>
    </main>
  );
}

function HealthCheckRow({ check, name }: { check: HealthCheck; name: string }) {
  const label = check.healthy ? "Available" : "Unavailable";
  const detail = check.path ?? check.executable ?? "No location reported";

  return (
    <div className="check-row">
      <dt>{name}</dt>
      <dd>
        <span className={`state state--${check.healthy ? "healthy" : "unavailable"}`}>
          {label}
        </span>
      </dd>
      <dd className="check-detail">{detail}</dd>
    </div>
  );
}

function LoadingSystemChecks() {
  return (
    <main className="workbench-page" id="main-content">
      <section aria-busy="true" aria-label="Loading system checks" className="loading-panel">
        <p className="eyebrow">Local prerequisites</p>
        <p role="status">Loading system checks</p>
        <div aria-hidden="true" className="loading-line" />
        <div aria-hidden="true" className="loading-line loading-line--short" />
      </section>
    </main>
  );
}

function SystemStatusError({ onRetry }: { onRetry: () => Promise<void> }) {
  return (
    <main className="workbench-page" id="main-content">
      <section className="message-panel" role="alert">
        <h1>Could not load system checks</h1>
        <p>The local health endpoint did not return a result. Check that the backend is running, then try again.</p>
        <button onClick={() => void onRetry()} type="button">
          Retry system checks
        </button>
      </section>
    </main>
  );
}
