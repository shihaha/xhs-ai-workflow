import type { ReactNode } from "react";

import { currentWorkbenchSection, workbenchNavGroups } from "./sidebar-data";

export function WorkbenchShell({ pathname, children }: { pathname: string; children: ReactNode }) {
  return (
    <div className="workbench-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <aside className="workbench-sidebar" aria-label="工作台侧栏">
        <div className="sidebar-brand">
          <a className="sidebar-brand__name" href="/radar">小红书 AI 工作台</a>
          <p className="sidebar-brand__meta">真实证据 · 人工门禁 · 本地运行</p>
        </div>
        <nav className="sidebar-nav" aria-label="工作台导航">
          {workbenchNavGroups.map(group => (
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
            <strong>{currentWorkbenchSection(pathname)}</strong>
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
