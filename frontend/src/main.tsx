import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AgentJobDetailPage, AgentRunDetailPage, AgentWorkbenchPage } from "./pages/AgentWorkbenchPage";
import { JobsPage } from "./pages/JobsPage";
import { AccountPage } from "./pages/AccountPage";
import { ContentStudioPage } from "./pages/ContentStudioPage";
import { OpportunitiesPage } from "./pages/OpportunitiesPage";
import { RadarPage } from "./pages/RadarPage";
import { SystemStatusPage } from "./pages/SystemStatusPage";
import "./styles.css";

export function App({ pathname = window.location.pathname }: { pathname?: string }) {
  const accountMatch = /^\/accounts\/([^/]+)$/.exec(pathname);
  const agentJobMatch = /^\/agent\/jobs\/([^/]+)$/.exec(pathname);
  const agentRunMatch = /^\/agent\/runs\/([^/]+)$/.exec(pathname);
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <header className="app-header">
        <a className="product-name" href="/status">小红书需求雷达工作台</a>
        <nav aria-label="工作台导航">
          <a aria-current={pathname === "/radar" ? "page" : undefined} href="/radar">需求雷达</a>
          <a aria-current={pathname.startsWith("/accounts/") ? "page" : undefined} href="/radar">账号证据</a>
          <a aria-current={pathname === "/opportunities" ? "page" : undefined} href="/opportunities">机会审核</a>
          <a aria-current={pathname === "/content" ? "page" : undefined} href="/content">内容工作台</a>
          <a aria-current={pathname === "/status" ? "page" : undefined} href="/status">系统状态</a>
          <a aria-current={pathname === "/agent" || pathname.startsWith("/agent/") ? "page" : undefined} href="/agent">Agent 工作台</a>
          <a aria-current={pathname === "/jobs" ? "page" : undefined} href="/jobs">任务记录</a>
        </nav>
      </header>
      {pathname === "/radar" ? <RadarPage /> : accountMatch ? <AccountPage accountId={decodeURIComponent(accountMatch[1])} /> : pathname === "/opportunities" ? <OpportunitiesPage /> : pathname === "/content" ? <ContentStudioPage /> : pathname === "/status" ? <SystemStatusPage /> : pathname === "/agent" ? <AgentWorkbenchPage /> : agentJobMatch ? <AgentJobDetailPage jobId={decodeURIComponent(agentJobMatch[1])} /> : agentRunMatch ? <AgentRunDetailPage runId={decodeURIComponent(agentRunMatch[1])} /> : pathname === "/jobs" ? <JobsPage /> : <NotFoundPage />}
    </div>
  );
}

function NotFoundPage() {
  return (
    <main className="workbench-page" id="main-content">
      <section className="message-panel">
        <h1>页面不存在</h1>
        <p>请从工作台导航中选择“系统状态”或“任务记录”。</p>
      </section>
    </main>
  );
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<StrictMode><App /></StrictMode>);
