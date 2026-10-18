import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { Building2, LogOut, Radio, User } from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { get } from "../api/client";
import { useAuth, type Domain } from "../auth/AuthContext";
import { StatusBadge } from "../components/StatusBadge";
import { Button } from "../components/ui";
import { useEventStream } from "../lib/events";
import { relativeTime } from "../lib/format";
import { NAV } from "./nav";

interface ProjectLite {
  id: string;
  name: string;
  project_key: string;
  domain: Domain;
}

function DomainSwitch() {
  const { domain, selectContext, session } = useAuth();
  const [busy, setBusy] = useState(false);
  const allowed = session?.allowed_domains ?? [];
  const nav = useNavigate();
  return (
    <div className="flex items-center gap-1 rounded-md border border-line p-0.5" role="group" aria-label="Domain">
      {(["PERSONAL", "OFFICE"] as Domain[]).map((d) => (
        <button
          key={d}
          disabled={busy || !allowed.includes(d)}
          onClick={async () => {
            if (d === domain) return;
            if (!window.confirm(`Pindah ke domain ${d}? Data domain lain tidak akan ditampilkan.`)) return;
            setBusy(true);
            try {
              await selectContext(d);
              nav("/");
            } finally {
              setBusy(false);
            }
          }}
          className={clsx(
            "flex items-center gap-1 rounded px-2 py-1 text-xs font-semibold",
            d === domain ? "bg-accent text-accent-ink" : "text-ink-2 hover:bg-surface-3",
          )}
          aria-pressed={d === domain}
        >
          {d === "PERSONAL" ? <User className="h-3.5 w-3.5" aria-hidden /> : <Building2 className="h-3.5 w-3.5" aria-hidden />}
          {d}
        </button>
      ))}
    </div>
  );
}

function ProjectSwitch() {
  const { domain, projectId, selectContext } = useAuth();
  const q = useQuery({
    queryKey: ["projects", "switch", domain],
    queryFn: () => get<{ items: ProjectLite[] }>("/api/projects"),
    enabled: !!domain,
    retry: false,
  });
  if (!domain) return null;
  const items = q.data?.items ?? [];
  return (
    <select
      aria-label="Proyek"
      className="max-w-56 rounded-md border border-line bg-surface px-2 py-1 text-xs"
      value={projectId ?? ""}
      onChange={(e) => selectContext(domain, e.target.value || null)}
    >
      <option value="">— pilih proyek —</option>
      {items.map((p) => (
        <option key={p.id} value={p.id}>
          {p.name}
        </option>
      ))}
    </select>
  );
}

export function AppShell() {
  const { session, logout, domain } = useAuth();
  const { status, lastSeenAt } = useEventStream(() => {}, !!session);
  const health = useQuery({ queryKey: ["health"], queryFn: () => get<{ environment: string; version: string }>("/api/health"), staleTime: 300_000 });
  return (
    <div className="flex h-full">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-line bg-surface md:flex">
        <div className="border-b border-line px-4 py-3">
          <div className="text-sm font-bold tracking-tight">DLY AEOS</div>
          <div className="text-xs text-ink-3">AI Engineering Operating System</div>
        </div>
        <nav className="flex-1 overflow-y-auto px-2 py-3" aria-label="Navigasi utama">
          {NAV.map((g) => (
            <div key={g.label} className="mb-3">
              <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-ink-3">{g.label}</div>
              {g.items.map((it) => (
                <NavLink
                  key={it.to}
                  to={it.to}
                  end={it.to === "/"}
                  title={it.question}
                  className={({ isActive }) =>
                    clsx("flex items-center gap-2 rounded-md px-2 py-1.5 text-sm", isActive ? "bg-surface-3 font-medium" : "text-ink-2 hover:bg-surface-2")
                  }
                >
                  <it.icon className="h-4 w-4" aria-hidden />
                  {it.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center gap-3 border-b border-line bg-surface px-4 py-2">
          <DomainSwitch />
          <ProjectSwitch />
          <div className="flex items-center gap-2 text-xs text-ink-3">
            <span>env: {health.data?.environment ?? "?"}</span>
            <span>v{health.data?.version ?? "?"}</span>
          </div>
          <div className="ml-auto flex items-center gap-3">
            <span className="inline-flex items-center gap-1 text-xs" title={lastSeenAt ? `Event terakhir ${relativeTime(lastSeenAt)}` : "Belum menerima event"}>
              <Radio className={clsx("h-3.5 w-3.5", status === "live" ? "text-ok" : "text-warn")} aria-hidden />
              {status === "live" ? "Stream live" : status === "reconnecting" ? "Menyambung ulang — data mungkin usang" : "Stream belum tersambung"}
            </span>
            <StatusBadge status={domain ? "LIVE" : "UNKNOWN"} label={domain ? "LIVE" : "Domain belum dipilih"} />
            <span className="text-xs text-ink-2">
              {session?.user.display_name} · {session?.user.role === "VIEWER" ? "VIEWER (baca saja)" : "SUPER_ADMIN"}
            </span>
            <Button variant="ghost" onClick={() => logout()} aria-label="Keluar">
              <LogOut className="h-4 w-4" aria-hidden />
            </Button>
          </div>
        </header>
        <main className="min-w-0 flex-1 overflow-y-auto p-5">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
