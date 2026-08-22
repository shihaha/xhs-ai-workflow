import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";

import { JobsPage } from "./pages/JobsPage";
import { AccountPage } from "./pages/AccountPage";
import { ContentStudioPage } from "./pages/ContentStudioPage";
import { OpportunitiesPage } from "./pages/OpportunitiesPage";
import { RadarPage } from "./pages/RadarPage";
import { SystemStatusPage } from "./pages/SystemStatusPage";
import "./styles.css";

type NavItem = {
  label: string;
  href: string;
  active: (pathname: string) => boolean;
  badge?: string;
};

type NavGroup = {
  title: string;
  items: NavItem[];
};

const navGroups: NavGroup[] = [
  {
    title: "需求雷达",
    items: [
      { label: "雷达总览", href: "/radar", active: pathname => pathname === "/radar" },
      { label: "账号证据", href: "/radar", active: pathname => pathname.startsWith("/accounts/") },
      { label: "机会审核", href: "/opportunities", active: pathname => pathname === "/opportunities", badge: "人工门" },
    ],
  },
  {
    title: "内容系统",
    items: [
      { label: "内容工作台", href: "/content", active: pathname => pathname === "/content" },
    ],
  },
  {
    title: "运行中心",
    items: [
      { label: "任务记录", href: "/jobs", active: pathname => pathname === "/jobs" },
      { label: "系统状态", href: "/status", active: pathname => pathname === "/status" },
    ],
  },
];

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

function WorkbenchShell({ pathname, children }: { pathname: string; children: ReactNode }) {
  return (
    <div className="workbench-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <aside className="workbench-sidebar" aria-label="工作台侧栏">
        <div className="sidebar-brand">
          <a className="sidebar-brand__name" href="/radar">小红书 AI 工作台</a>
          <p className="sidebar-brand__meta">真实证据 · 人工门禁 · 本地运行</p>
        </div>
        <nav className="sidebar-nav" aria-label="工作台导航">
          {navGroups.map(group => (
            <section className="sidebar-nav-group" key={group.title} aria-labelledby={`nav-${group.title}`}>
              <h2 id={`nav-${group.title}`} className="sidebar-nav-group__title">{group.title}</h2>
              <div className="sidebar-nav-group__items">
                {group.items.map(item => {
                  const active = item.active(pathname);
                  return (
                    <a className={`sidebar-nav-item${active ? " is-active" : ""}`} aria-current={active ? "page" : undefined} href={item.href} key={`${group.title}-${item.label}`}>
                      <span>{item.label}</span>
                      {item.badge ? <span className="sidebar-nav-item__badge">{item.badge}</span> : null}
                    </a>
                  );
                })}
              </div>
            </section>
          ))}
        </nav>
        <div className="sidebar-boundary" role="note">
          <strong>系统边界</strong>
          <span>产品研究与产品制作按具体商品独立执行，不在本工作台自动化。</span>
        </div>
      </aside>
      <div className="workbench-stage">
        <header className="workbench-topbar">
          <div>
            <p className="workbench-topbar__eyebrow">XHS Intelligence Workbench</p>
            <strong>{currentSection(pathname)}</strong>
          </div>
          <div className="workbench-topbar__meta" aria-label="运行约束">
            <span className="topbar-chip">本地单用户</span>
            <span className="topbar-chip">不自动发布</span>
          </div>
        </header>
        <div className="workbench-stage__content">{children}</div>
      </div>
    </div>
  );
}

function currentSection(pathname: string): string {
  if (pathname.startsWith("/accounts/")) return "账号证据";
  for (const group of navGroups) {
    const item = group.items.find(entry => entry.active(pathname));
    if (item) return item.label;
  }
  return "工作台";
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
