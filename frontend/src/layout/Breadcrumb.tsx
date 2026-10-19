import { ChevronRight } from "lucide-react";
import { Link, useLocation } from "react-router-dom";
import { NAV } from "./nav";

/** Detail routes and the list page they belong to (the list page is in the navigation). */
const DETAIL_PARENTS: { prefix: string; parent: string }[] = [
  { prefix: "/tools/", parent: "/tools" },
  { prefix: "/runs/", parent: "/work" },
  { prefix: "/plans/", parent: "/plans" },
  { prefix: "/proposals/", parent: "/proposals" },
  { prefix: "/projects/", parent: "/projects" },
];

export interface Crumb {
  label: string;
  to?: string;
}

/** Navigation group -> page -> detail for a path, derived from NAV (§35 "breadcrumb" as a diagram alternative). */
export function crumbsFor(pathname: string, detailLabel?: string): Crumb[] {
  const detail = DETAIL_PARENTS.find((d) => pathname.startsWith(d.prefix) && pathname.length > d.prefix.length);
  const pagePath = detail ? detail.parent : pathname;
  for (const group of NAV) {
    const item = group.items.find((i) => i.to === pagePath);
    if (!item) continue;
    const crumbs: Crumb[] = [{ label: group.label }, { label: item.label, to: item.to }];
    if (detail) crumbs.push({ label: detailLabel ?? decodeURIComponent(pathname.slice(detail.prefix.length)) });
    return crumbs;
  }
  return [];
}

export function Breadcrumb({ detailLabel }: { detailLabel?: string }) {
  const { pathname } = useLocation();
  const crumbs = crumbsFor(pathname, detailLabel);
  if (crumbs.length < 3) return null; // list pages already show their place in the sidebar; details get a trail
  return (
    <nav aria-label="Breadcrumb" className="mb-2 text-xs text-ink-3">
      <ol className="flex flex-wrap items-center gap-1">
        {crumbs.map((c, i) => (
          <li key={`${c.label}-${i}`} className="flex items-center gap-1">
            {i > 0 && <ChevronRight className="h-3 w-3" aria-hidden />}
            {c.to && i < crumbs.length - 1 ? (
              <Link to={c.to} className="text-accent hover:underline">
                {c.label}
              </Link>
            ) : (
              <span aria-current={i === crumbs.length - 1 ? "page" : undefined}>{c.label}</span>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}
