import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { JobsPage } from "./pages/JobsPage";
import { SystemStatusPage } from "./pages/SystemStatusPage";
import "./styles.css";

export function App({ pathname = window.location.pathname }: { pathname?: string }) {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <header className="app-header">
        <a className="product-name" href="/status">XHS Intelligence Workbench</a>
        <nav aria-label="Workbench">
          <a aria-current={pathname === "/status" ? "page" : undefined} href="/status">System status</a>
          <a aria-current={pathname === "/jobs" ? "page" : undefined} href="/jobs">Jobs</a>
        </nav>
      </header>
      {pathname === "/status" ? <SystemStatusPage /> : pathname === "/jobs" ? <JobsPage /> : <NotFoundPage />}
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
if (!root) throw new Error("The workbench root element is missing.");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
