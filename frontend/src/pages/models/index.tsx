import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, Freshness, Loading, Mono, Notice, PageHeader, Table, inputClass } from "../../components/ui";
import { relativeTime } from "../../lib/format";
import { useEventStream } from "../../lib/events";
import { CopyCommand } from "../integrations/CopyCommand";
import { useRefreshCatalog, useRoutes } from "./api";
import { Badge } from "./badges";
import { RouteDrawer, freeLabel } from "./RouteDrawer";
import { SupervisorSection } from "./SupervisorSection";
import { POOL_LABELS, type RouteItem } from "./types";

const POOLS = ["WORKER", "COMPARATOR", "ARENA", "WATCHLIST", "LOCAL"];
const STATUSES = ["CANDIDATE", "UNTESTED", "TRIAL", "APPROVED", "DISABLED"];

export default function ModelsPage() {
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const routes = useRoutes();
  const refresh = useRefreshCatalog();
  const [pool, setPool] = useState("");
  const [status, setStatus] = useState("");
  const [domain, setDomain] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  useEventStream((e) => {
    if (e.source === "routing") qc.invalidateQueries({ queryKey: ["routes"] });
    if (e.source === "claude_bridge") qc.invalidateQueries({ queryKey: ["claude-status"] });
  });

  const items = useMemo(
    () =>
      (routes.data?.items ?? []).filter(
        (r) =>
          r.pool !== "SUPERVISOR" &&
          (!pool || r.pool === pool) &&
          (!status || r.status === status) &&
          (!domain || r.eligible_domains.includes(domain)),
      ),
    [routes.data, pool, status, domain],
  );

  const columns = [
    { header: "Pool", cell: (r: RouteItem) => <span className="text-xs">{POOL_LABELS[r.pool] ?? r.pool}</span> },
    { header: "Penyedia", cell: (r: RouteItem) => r.provider },
    {
      header: "Model ID persis",
      cell: (r: RouteItem) => (
        <div className="min-w-0">
          <div className="font-medium">{r.display_name}</div>
          <Mono>{r.model_id}</Mono>
          {r.identity_status === "UNVERIFIED" && (
            <div className="mt-0.5">
              <StatusBadge status="UNVERIFIED" label="Identitas belum terbuka" />
            </div>
          )}
        </div>
      ),
    },
    {
      header: "Versi teresolusi",
      cell: (r: RouteItem) => (r.resolved_version === "UNKNOWN" ? <StatusBadge status="UNKNOWN" /> : <Mono>{r.resolved_version}</Mono>),
    },
    { header: "Gratis/berbayar", cell: (r: RouteItem) => <div className="space-y-0.5">{freeLabel(r)}{r.expires_note && <div className="text-xs text-ink-3">{r.expires_note}</div>}</div> },
    {
      header: "Data terms",
      cell: (r: RouteItem) => (
        <div className="space-y-0.5 text-xs">
          <div>Kelas: {r.allowed_data_classes.join(", ") || "—"}</div>
          <Freshness at={r.terms_checked_at} label="Diperiksa" staleAfterSeconds={30 * 86400} />
        </div>
      ),
    },
    { header: "Domain", cell: (r: RouteItem) => r.eligible_domains.join(", ") || "—" },
    {
      header: "Status",
      cell: (r: RouteItem) => (
        <div className="flex flex-col gap-1">
          <StatusBadge status={r.status} />
          {r.mode !== "LIVE" && <StatusBadge status={r.mode} />}
          {r.quota.state === "EXHAUSTED" && <Badge status="EXHAUSTED" />}
        </div>
      ),
    },
    {
      header: "Evaluasi",
      cell: (r: RouteItem) =>
        r.eval_summary.state === "DIUJI" ? (
          <span className="text-xs">{Object.keys(r.eval_summary.roles).join(", ")}</span>
        ) : (
          <StatusBadge status="NOT_TESTED" label="BELUM DIUJI" />
        ),
    },
    {
      header: "Terakhir dipakai",
      cell: (r: RouteItem) => (r.last_used?.at ? <span className="text-xs">{relativeTime(r.last_used.at)}</span> : <StatusBadge status="NEVER_OBSERVED" />),
    },
  ];

  const lastRefresh = routes.data?.catalog_refresh;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Model & Rute"
        question="Model mana yang tersedia, diizinkan dan benar-benar mengerjakan tugas?"
        source={routes.data?.source ?? "registry model_routes + status Claude Code resmi"}
        checkedAt={routes.data?.checked_at ?? null}
        evidenceLimit="Katalog = E1 (bacaan dokumen); CANDIDATE/BELUM DIUJI bukan teruji; kuota Max UNKNOWN"
        mode={routes.data?.mode}
        relatedDecisionsHref="/decisions?target_type=model_route"
        actions={
          isAdmin ? (
            <Button busy={refresh.isPending} onClick={() => refresh.mutate()}>
              <RefreshCw className="h-4 w-4" aria-hidden /> Perbarui katalog
            </Button>
          ) : undefined
        }
      />
      <SupervisorSection />
      <Card
        title="Registry rute pekerja"
        actions={
          lastRefresh?.at ? (
            <span className="flex items-center gap-2 text-xs">
              Katalog: <Badge status={lastRefresh.status === "OK" ? "CONNECTED" : lastRefresh.status === "PARTIAL" ? "PARTIAL" : "FAILED"} label={lastRefresh.status} />
              <Freshness at={lastRefresh.at} label="Diperbarui" staleAfterSeconds={7 * 86400} />
            </span>
          ) : (
            <span className="text-xs text-ink-3">Katalog belum pernah diperbarui</span>
          )
        }
      >
        <Notice tone="mock">
          Entri bertanda DESIGN berasal dari master §23 dan katalog penyedia (E1). Tidak ada rute yang disetujui oleh seed; OFFICE
          selalu ditolak sampai otorisasi perusahaan tercatat; tidak ada fallback berbayar otomatis.
        </Notice>
        {refresh.error && <div className="mt-3"><ErrorBox error={refresh.error} /></div>}
        {refresh.data && (
          <div className="mt-3">
            <Notice tone={refresh.data.status === "OK" ? "info" : "warn"}>
              Hasil pembaruan: {refresh.data.status}.{" "}
              {Object.entries(refresh.data.results).map(([k, v]) => `${k}: ${v.status}${v.reason ? ` (${v.reason})` : ""}`).join(" · ")}
            </Notice>
          </div>
        )}
        <div className="my-4 grid gap-3 sm:grid-cols-3">
          <Field label="Pool">
            <select className={inputClass} value={pool} onChange={(e) => setPool(e.target.value)} aria-label="Filter pool">
              <option value="">Semua</option>
              {POOLS.map((p) => <option key={p} value={p}>{POOL_LABELS[p]}</option>)}
            </select>
          </Field>
          <Field label="Status">
            <select className={inputClass} value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter status">
              <option value="">Semua</option>
              {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </Field>
          <Field label="Domain">
            <select className={inputClass} value={domain} onChange={(e) => setDomain(e.target.value)} aria-label="Filter domain">
              <option value="">Semua</option>
              <option value="PERSONAL">PERSONAL</option>
              <option value="OFFICE">OFFICE</option>
            </select>
          </Field>
        </div>
        {routes.isLoading && <Loading />}
        {routes.error && <ErrorBox error={routes.error} />}
        {routes.data && routes.data.items.length === 0 && (
          <EmptyState title="Registry model kosong">
            <div className="mt-2 flex justify-center">
              <CopyCommand command="aeos seed --only aeos.routing.seed" why="Muat katalog desain dari master §23" />
            </div>
          </EmptyState>
        )}
        {routes.data && routes.data.items.length > 0 && (
          <Table rows={items} columns={columns} rowKey={(r) => r.route_key} onRowClick={(r) => setSelected(r.route_key)} empty="Tidak ada rute untuk filter ini" />
        )}
      </Card>
      {selected && <RouteDrawer routeKey={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
