import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { JobsPage } from "./pages/JobsPage";
import { AccountPage } from "./pages/AccountPage";
import { ContentStudioPage } from "./pages/ContentStudioPage";
import { OpportunitiesPage } from "./pages/OpportunitiesPage";
import { RadarPage } from "./pages/RadarPage";
import { SystemStatusPage } from "./pages/SystemStatusPage";
import "./styles.css";

export function App({ pathname = window.location.pathname }: { pathname?: string }) {
  const accountMatch = /^\/accounts\/([^/]+)$/.exec(pathname);
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <header className="app-header">
        <a className="product-name" href="/status">XHS Intelligence Workbench</a>
        <nav aria-label="Workbench">
          <a aria-current={pathname === "/radar" ? "page" : undefined} href="/radar">Radar</a>
          <a aria-current={pathname.startsWith("/accounts/") ? "page" : undefined} href="/radar">Accounts</a>
          <a aria-current={pathname === "/opportunities" ? "page" : undefined} href="/opportunities">Opportunities</a>
          <a aria-current={pathname === "/content" ? "page" : undefined} href="/content">Content studio</a>
          <a aria-current={pathname === "/status" ? "page" : undefined} href="/status">System status</a>
          <a aria-current={pathname === "/jobs" ? "page" : undefined} href="/jobs">Jobs</a>
        </nav>
      </header>
      {pathname === "/radar" ? <RadarPage /> : accountMatch ? <AccountPage accountId={decodeURIComponent(accountMatch[1])} /> : pathname === "/opportunities" ? <OpportunitiesPage /> : pathname === "/content" ? <ContentStudioPage /> : pathname === "/status" ? <SystemStatusPage /> : pathname === "/jobs" ? <JobsPage /> : <NotFoundPage />}
    </div>
  );
}

function NotFoundPage() {
  return (
    <main className="workbench-page" id="main-content">
      <section className="message-panel">
        <h1>Route not found</h1>
        <p>Choose System status or Jobs from the workbench navigation.</p>
      </section>
    </main>
  );
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<StrictMode><App /></StrictMode>);
