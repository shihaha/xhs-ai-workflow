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
  const overallLabel = health.status === "healthy" ? "全部正常" : "部分功能不可用";

  return (
    <main className="workbench-page" id="main-content">
      <header className="page-heading">
        <p className="eyebrow">本机运行条件</p>
        <h1>系统状态</h1>
        <p>这里显示本机接口返回的实时检查结果，不代表外部平台一定可用。</p>
      </header>

      <section aria-labelledby="overall-status-heading" className="status-summary">
        <h2 id="overall-status-heading">总体状态</h2>
        <p className={`state state--${health.status}`} role="status">
          {overallLabel}
        </p>
      </section>

      <section aria-labelledby="checks-heading" className="operator-panel">
        <div className="panel-heading">
          <h2 id="checks-heading">检查项目</h2>
          <p>共返回 {checks.length} 项检查</p>
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
  const label = check.healthy ? "可用" : "不可用";
  const detail = check.path ?? check.executable ?? "没有提供路径";
  const displayName = ({
    database: "数据库",
    adb: "Android设备",
    browser: "浏览器",
    bailian: "百炼",
    bailian_text: "百炼文本模型",
    bailian_vision: "百炼视觉模型",
    bailian_image: "百炼图片模型",
  } as Record<string, string>)[name] ?? name;

  return (
    <div className="check-row">
      <dt>{displayName}</dt>
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
      <section aria-busy="true" aria-label="正在检查系统状态" className="loading-panel">
        <p className="eyebrow">本机运行条件</p>
        <p role="status">正在检查系统状态</p>
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
        <h1>无法加载系统状态</h1>
        <p>本机健康检查接口没有返回结果。请确认后端正在运行，然后重新检查。</p>
        <button onClick={() => void onRetry()} type="button">
          重新检查
        </button>
      </section>
    </main>
  );
}
