import { useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import { Building2, LogOut, Menu, Moon, Radio, Sun, User, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { get } from "../api/client";
import { useAuth, type Domain } from "../auth/AuthContext";
import { StatusBadge } from "../components/StatusBadge";
import { Button } from "../components/ui";
import { useEventStream, type StreamStatus } from "../lib/events";
import { absoluteTime, relativeTime } from "../lib/format";
import { useTheme } from "../lib/theme";
import type { Overview } from "../pages/overview/types";
import { GlobalSearch } from "./GlobalSearch";
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
    <div className="flex shrink-0 items-center gap-1 rounded-md border border-line p-0.5" role="group" aria-label="Domain">
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
      title="Proyek aktif (dipilih manusia, dicek ulang di backend)"
      className="min-w-0 flex-1 rounded-md border border-line bg-surface px-2 py-1 text-xs md:max-w-56 md:flex-none"
      value={projectId ?? ""}
      onChange={(e) => selectContext(domain, e.target.value || null)}
    >
      <option value="">{items.length ? "— pilih proyek —" : "— belum ada proyek di domain ini —"}</option>
      {items.map((p) => (
        <option key={p.id} value={p.id}>
          {p.name}
        </option>
      ))}
    </select>
  );
}

const MODE_LABEL: Record<string, string> = {
  LIVE: "LIVE",
  MIXED: "LIVE + DEMO",
  MOCK: "MOCK",
  DEMO: "DEMO",
};

/** §35 header: environment, data freshness and LIVE/MOCK mode, computed from records (never assumed). */
function ContextStatus({ stream, lastSeenAt, className }: { stream: StreamStatus; lastSeenAt: string | null; className?: string }) {
  const { domain, projectId } = useAuth();
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => get<{ environment: string; version: string }>("/api/health"),
    staleTime: 300_000,
  });
  // Same key and endpoint as the Ringkasan page, so both share one cached projection.
  const ov = useQuery({
    queryKey: ["cc-overview", domain, projectId],
    queryFn: () => get<Overview>("/api/control-center/overview"),
    enabled: !!domain,
    refetchInterval: 30_000,
    retry: false,
  });
  const env = ov.data?.environment ?? health.data?.environment;
  const mode = ov.data?.mode;
  const mockRows = Object.values(ov.data?.mock_rows ?? {}).reduce((a, b) => a + b, 0);
  const streamOk = stream === "live";
  return (
    <div className={clsx("flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-3", className)}>
      <span data-testid="environment" title={`Versi ${health.data?.version ?? "?"}`}>
        Lingkungan: <span className="mono text-ink-2">{env ?? "tidak diketahui"}</span>
      </span>
      <span data-testid="data-mode" title={mode === "MIXED" ? `${mockRows} baris MOCK/DEMO berlabel di domain ini; tidak dihitung sebagai hasil live` : "Mode data dari proyeksi Control Center"}>
        <StatusBadge status={mode ?? "UNKNOWN"} label={mode ? MODE_LABEL[mode] ?? mode : "Mode tidak diketahui"} />
      </span>
      <span
        data-testid="freshness"
        className={clsx("inline-flex items-center gap-1", streamOk ? "text-ink-2" : "text-warn")}
        title={`Proyeksi dibuat ${absoluteTime(ov.data?.generated_at)}; event terakhir ${lastSeenAt ? absoluteTime(lastSeenAt) : "belum ada"}`}
      >
        <Radio className={clsx("h-3.5 w-3.5", streamOk ? "text-ok" : "text-warn")} aria-hidden />
        {streamOk
          ? `Stream live · data ${ov.data ? relativeTime(ov.data.generated_at) : "belum dimuat"}`
          : stream === "reconnecting"
            ? `Stream terputus — data mungkin usang${lastSeenAt ? ` (terakhir ${relativeTime(lastSeenAt)})` : ""}`
            : "Stream belum tersambung — kesegaran tidak diketahui"}
      </span>
    </div>
  );
}

function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const label = theme === "dark" ? "Tema gelap aktif — ganti ke terang" : "Tema terang aktif — ganti ke gelap";
  return (
    <Button variant="ghost" onClick={toggle} aria-label={label} title={label} className="px-2">
      {theme === "dark" ? <Sun className="h-4 w-4" aria-hidden /> : <Moon className="h-4 w-4" aria-hidden />}
    </Button>
  );
}

function NavList({ label, pending, onNavigate }: { label: string; pending: number; onNavigate?: () => void }) {
  return (
    <nav className="flex-1 overflow-y-auto px-2 py-3" aria-label={label}>
      {NAV.map((g) => (
        <div key={g.label} className="mb-3">
          <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-ink-3">{g.label}</div>
          {g.items.map((it) => (
            <NavLink
              key={it.to}
              to={it.to}
              end={it.to === "/"}
              title={it.question}
              onClick={onNavigate}
              className={({ isActive }) =>
                clsx("flex items-center gap-2 rounded-md px-2 py-1.5 text-sm", isActive ? "bg-surface-3 font-medium" : "text-ink-2 hover:bg-surface-2")
              }
            >
              <it.icon className="h-4 w-4 shrink-0" aria-hidden />
              <span className="min-w-0 flex-1 truncate">{it.label}</span>
              {it.to === "/decisions" && pending > 0 && (
                <span
                  data-testid="nav-decisions-count"
                  className="rounded-full bg-warn-bg px-1.5 text-[11px] font-semibold text-warn"
                  title={`${pending} keputusan manusia menunggu (antrean /api/decisions/queue)`}
                >
                  {pending}
                  <span className="sr-only"> keputusan menunggu</span>
                </span>
              )}
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}

function Brand() {
  return (
    <div className="border-b border-line px-4 py-3">
      <div className="text-sm font-bold tracking-tight">DLY AEOS</div>
      <div className="text-xs text-ink-3">AI Engineering Operating System</div>
    </div>
  );
}

/** Below md the sidebar is hidden; this drawer carries the same navigation (§35, 390px phones). */
function MobileNav({ open, onClose, pending }: { open: boolean; onClose: () => void; pending: number }) {
  const closeBtn = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    closeBtn.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 md:hidden">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} aria-hidden />
      <div role="dialog" aria-modal="true" aria-label="Navigasi" className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col bg-surface shadow-xl">
        <div className="flex items-start justify-between">
          <Brand />
          <Button ref={closeBtn} variant="ghost" onClick={onClose} aria-label="Tutup menu navigasi" className="m-2 px-2">
            <X className="h-4 w-4" aria-hidden />
          </Button>
        </div>
        <NavList label="Navigasi seluler" pending={pending} onNavigate={onClose} />
      </div>
    </div>
  );
}

const QUEUE_EVENT = /decision|proposal|plan|publication|task|run\./;

export function AppShell() {
  const { session, logout, domain } = useAuth();
  const qc = useQueryClient();
  const [navOpen, setNavOpen] = useState(false);
  const menuBtn = useRef<HTMLButtonElement>(null);
  const queueTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { status, lastSeenAt } = useEventStream((e) => {
    if (!QUEUE_EVENT.test(e.kind) || queueTimer.current) return;
    // Debounced: a burst of run/task events refreshes the decision count once.
    queueTimer.current = setTimeout(() => {
      queueTimer.current = null;
      qc.invalidateQueries({ queryKey: ["decision-queue"] });
    }, 750);
  }, !!session);
  useEffect(
    () => () => {
      if (queueTimer.current) clearTimeout(queueTimer.current);
    },
    [],
  );
  // Same key as the Keputusan page; the count is the human decision queue, never a model's opinion.
  const queue = useQuery({
    queryKey: ["decision-queue", domain],
    queryFn: () => get<{ items: unknown[] }>("/api/decisions/queue"),
    enabled: !!domain,
    refetchInterval: 30_000,
    retry: false,
  });
  const pending = queue.data?.items.length ?? 0;
  const closeNav = useCallback(() => {
    setNavOpen(false);
    menuBtn.current?.focus();
  }, []);
  const isViewer = session?.user.role === "VIEWER";
  return (
    <div className="flex min-h-full">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col self-start border-r border-line bg-surface md:flex">
        <Brand />
        <NavList label="Navigasi utama" pending={pending} />
      </aside>
      <MobileNav open={navOpen} onClose={closeNav} pending={pending} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 border-b border-line bg-surface">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2 md:px-4">
            <Button ref={menuBtn} variant="ghost" className="px-2 md:hidden" onClick={() => setNavOpen(true)} aria-label="Buka menu navigasi" aria-expanded={navOpen}>
              <Menu className="h-5 w-5" aria-hidden />
            </Button>
            <span className="text-sm font-bold tracking-tight md:hidden">DLY AEOS</span>
            <div className="order-3 flex w-full min-w-0 items-center gap-2 md:order-none md:w-auto">
              <DomainSwitch />
              <ProjectSwitch />
            </div>
            <GlobalSearch className="order-4 w-full md:order-none md:ml-auto md:w-72 2xl:w-96" />
            <div className="ml-auto flex items-center gap-1 md:ml-0">
              <ThemeToggle />
              <span className="hidden text-xs text-ink-2 sm:inline">
                {session?.user.display_name} · {isViewer ? "VIEWER (baca saja)" : "SUPER_ADMIN"}
              </span>
              <span className="text-xs text-ink-2 sm:hidden" title={session?.user.display_name}>
                {isViewer ? "VIEWER" : "ADMIN"}
              </span>
              <Button variant="ghost" className="px-2" onClick={() => logout()} aria-label="Keluar" title="Keluar">
                <LogOut className="h-4 w-4" aria-hidden />
              </Button>
            </div>
            {/* Context bar: always its own row, so its varying freshness text never reflows the controls. */}
            <ContextStatus stream={status} lastSeenAt={lastSeenAt} className="order-5 w-full" />
          </div>
        </header>
        <main className="min-w-0 flex-1 p-4 md:p-5">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
