export type WorkbenchNavItem = {
  label: string;
  href: string;
  active: (pathname: string) => boolean;
  badge?: string;
};

export type WorkbenchNavGroup = {
  title: string;
  items: WorkbenchNavItem[];
};

export const workbenchNavGroups: WorkbenchNavGroup[] = [
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
      { label: "成品资料", href: "/content/research", active: pathname => pathname === "/content/research" },
      { label: "内容生产", href: "/content", active: pathname => pathname === "/content" },
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

export function currentWorkbenchSection(pathname: string): string {
  if (pathname.startsWith("/accounts/")) return "账号证据";
  for (const group of workbenchNavGroups) {
    const item = group.items.find(entry => entry.active(pathname));
    if (item) return item.label;
  }
  return "工作台";
}
