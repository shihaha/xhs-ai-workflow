import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { WorkbenchShell } from "./components/layout/WorkbenchShell";
import { JobsPage } from "./pages/JobsPage";
import { AccountPage } from "./pages/AccountPage";
import { ContentStudioPage } from "./pages/ContentStudioPage";
import { OpportunitiesPage } from "./pages/OpportunitiesPage";
import { RadarPage } from "./pages/RadarPage";
import { SystemStatusPage } from "./pages/SystemStatusPage";
import "./styles.css";
import "./workbench-shell.css";

export function App({ pathname = window.location.pathname }: { pathname?: string }) {
  const accountMatch = /^\/accounts\/([^/]+)$/.exec(pathname);
  const page = pathname === "/radar"
    ? <RadarPage />
    : accountMatch
      ? <AccountPage accountId={decodeURIComponent(accountMatch[1])} />
      : pathname === "/opportunities"
        ? <OpportunitiesPage />
        : pathname === "/content"
          ? <ContentStudioPage />
          : pathname === "/status"
            ? <SystemStatusPage />
            : pathname === "/jobs"
              ? <JobsPage />
              : <NotFoundPage />;

  return <WorkbenchShell pathname={pathname}>{page}</WorkbenchShell>;
}

function NotFoundPage() {
  return (
    <main className="workbench-page" id="main-content">
      <section className="message-panel">
        <h1>页面不存在</h1>
        <p>请从左侧工作台导航选择可用模块。</p>
      </section>
    </main>
  );
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<StrictMode><App /></StrictMode>);
