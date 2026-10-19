import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import type { DagView, PlanSummary } from "../../components/planning/types";
import { StatusBadge } from "../../components/StatusBadge";
import { Card, EmptyState, ErrorBox, inputClass, Loading, Mono, Notice, PageHeader, Table, Tabs } from "../../components/ui";
import { useEventStream } from "../../lib/events";
import { relativeTime, shortHash } from "../../lib/format";
import { acceptanceBadge, runtimeBadge } from "./dagStatus";

// React Flow is only loaded when the graph tab is shown.
const DagGraph = lazy(() => import("./DagGraph").then((m) => ({ default: m.DagGraph })));

function TaskTable({ view, highlight }: { view: DagView; highlight: string | null }) {
  return (
    <Table
      rows={view.nodes}
      rowKey={(n) => n.key}
      empty="Tidak ada task pada versi ini"
      columns={[
        {
          header: "Task",
          cell: (n) => (
            <div className={highlight === n.key ? "font-semibold text-accent" : undefined}>
              <Mono>{n.key}</Mono> {n.title} {n.mode && n.mode !== "LIVE" && <StatusBadge status={n.mode} />}
              <div className="text-xs text-ink-3">{n.role_key} · risiko {n.risk_class} · {n.data_classification}</div>
              {n.warnings.map((w) => (
                <div key={w} className="text-xs text-warn">
                  {w}
                </div>
              ))}
            </div>
          ),
        },
        { header: "Bergantung pada", cell: (n) => (n.depends_on.length ? n.depends_on.join(", ") : "—") },
        {
          header: "Runtime",
          cell: (n) => {
            const r = runtimeBadge(n.runtime.state);
            return (
              <div>
                <StatusBadge status={r.badge} label={r.label} />
                {n.runtime.reason && <div className="text-xs text-ink-3">{n.runtime.reason}</div>}
              </div>
            );
          },
        },
        { header: "Lease", cell: (n) => (n.lease ? <span><Mono>{n.lease.owner}</Mono> s.d. {relativeTime(n.lease.expires_at)}</span> : "—") },
        {
          header: "Penerimaan",
          cell: (n) => {
            const a = acceptanceBadge(n.acceptance);
            return <StatusBadge status={a.badge} label={a.label} />;
          },
        },
        {
          header: "Prasyarat diterima",
          cell: (n) =>
            n.depends_on.length ? (
              <div className="text-xs">
                {n.accepted_prerequisites.length}/{n.depends_on.length}
                {n.dependencies_ready ? " — siap" : " — menunggu AcceptanceRecord"}
                {n.accepted_prerequisites.length > 0 && <div className="text-ink-3">Diterima: {n.accepted_prerequisites.join(", ")}</div>}
              </div>
            ) : (
              "tidak ada"
            ),
        },
        { header: "Path tulis", cell: (n) => <div className="text-xs">{n.allowed_paths.join(", ") || "—"}</div> },
        { header: "Konflik tulis", cell: (n) => (n.conflicts_with.length ? <span className="text-warn">{n.conflicts_with.join(", ")}</span> : "—") },
      ]}
    />
  );
}

function BlockersAndConflicts({ view }: { view: DagView }) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card title="Blocker kritis">
        {view.critical_blockers.length ? (
          <ul className="grid gap-2 text-sm">
            {view.critical_blockers.map((b) => (
              <li key={b.task_key} className="rounded border border-line p-2">
                <div className="flex items-center gap-2">
                  <Mono>{b.task_key}</Mono> <StatusBadge status={b.state} />
                </div>
                <div className="text-xs text-ink-2">{b.reason || "—"}</div>
                <div className="text-xs">Menahan {b.impact} task: {b.blocks.join(", ") || "—"}</div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-sm text-ink-3">Tidak ada task BLOCKED/FAILED pada versi ini.</div>
        )}
      </Card>
      <Card title="Konflik tulis bersama (shared-write)">
        {view.shared_write_conflicts.length ? (
          <ul className="grid gap-2 text-sm">
            {view.shared_write_conflicts.map((c) => (
              <li key={c.tasks.join("-")} className="rounded border border-line p-2">
                <div className="flex items-center gap-2">
                  <Mono>{c.tasks.join(" ↔ ")}</Mono>
                  {c.concurrent_risk ? <StatusBadge status="CONFLICT" label="Berisiko paralel — scheduler harus serialisasi" /> : <StatusBadge status="PENDING" label="Sudah diserialisasi dependensi" />}
                </div>
                <div className="text-xs text-ink-3">{c.paths.join("; ")}</div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-sm text-ink-3">Tidak ada path tulis yang tumpang tindih.</div>
        )}
      </Card>
    </div>
  );
}

export default function DagPage() {
  const { domain } = useAuth();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const plans = useQuery({ queryKey: ["plans", domain, "dag"], queryFn: () => get<{ items: PlanSummary[] }>("/api/plans") });
  const withVersion = (plans.data?.items ?? []).filter((p) => p.current_version);
  const planId = params.get("plan") ?? withVersion[0]?.id ?? null;
  const highlight = params.get("task");
  const dag = useQuery({
    queryKey: ["plan", planId, "dag"],
    queryFn: () => get<DagView>(`/api/plans/${planId}/dag`),
    enabled: !!planId,
  });
  useEventStream((e) => {
    if (planId && (e.kind.startsWith("task.") || e.kind.startsWith("run.") || e.kind.startsWith("plan."))) qc.invalidateQueries({ queryKey: ["plan", planId, "dag"] });
  });
  const view = dag.data;

  return (
    <div>
      <PageHeader
        title="Task DAG"
        question="Mengapa pekerjaan menunggu dan apa yang terdampak?"
        source="task_specs, task_dependencies, task_executions, leases, acceptance_records"
        checkedAt={view?.checked_at ?? null}
        evidenceLimit="State runtime dari scheduler; penerimaan hanya dari AcceptanceRecord. Status issue eksternal tidak membuka dependensi."
        mode={view?.data_mode && view.data_mode !== "LIVE" ? view.data_mode : view?.mode && view.mode !== "LIVE" ? view.mode : undefined}
      />
      <div className="mb-4 max-w-xl">
        <select
          aria-label="Pilih rencana"
          className={inputClass}
          value={planId ?? ""}
          onChange={(e) => setParams(e.target.value ? { plan: e.target.value } : {})}
        >
          {!withVersion.length && <option value="">— belum ada rencana dengan versi —</option>}
          {withVersion.map((p) => (
            <option key={p.id} value={p.id}>
              {p.title} — {p.approved_version ? `v${p.approved_version.version} disetujui` : `v${p.current_version?.version} (belum disetujui)`}
            </option>
          ))}
        </select>
      </div>
      {plans.isLoading && <Loading />}
      <ErrorBox error={plans.error ?? dag.error} />
      {plans.data && !withVersion.length && (
        <EmptyState title="Belum ada DAG">
          Buat dan jalankan rencana di halaman <Link to="/plans" className="underline">Rencana</Link>; DAG dikompilasi saat versi disetujui. Via CLI:{" "}
          <Mono>aeos plan create --project &lt;key&gt; --domain {domain ?? "PERSONAL"} --title "..." --prompt "..." --actor &lt;username&gt;</Mono> lalu{" "}
          <Mono>aeos plan run &lt;plan_id&gt; --actor &lt;username&gt;</Mono>.
        </EmptyState>
      )}
      {dag.isLoading && <Loading />}
      {view && (
        <div className="grid gap-4">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {view.version !== undefined && (
              <span>
                Versi v{view.version} <Mono>{shortHash(view.manifest_hash)}</Mono>
              </span>
            )}
            {view.approved ? <StatusBadge status="APPROVED" label="Versi disetujui" /> : <StatusBadge status="UNVERIFIED" label="Belum disetujui" />}
            {view.compiled ? <StatusBadge status="CONFIGURED" label="TaskSpec terkompilasi (immutable)" /> : <StatusBadge status="DESIGN" label="Pratinjau dari isi rencana" />}
            <Link to={`/plans/${view.plan_id}`} className="underline">
              Buka rencana
            </Link>
          </div>
          {view.data_mode && view.data_mode !== "LIVE" && (
            <Notice tone="mock">
              Isi rencana ini berlabel {view.data_mode} (bukan output model nyata). TaskSpec-nya tidak dihitung sebagai pekerjaan nyata dan tidak dipublikasikan.
            </Notice>
          )}
          {view.runtime_note && <Notice tone="info">{view.runtime_note}</Notice>}
          {view.validation_errors.length > 0 && (
            <Notice tone="warn">DAG tidak valid: {view.validation_errors.map((e) => String(e.code)).join(", ")}. Persetujuan dan kompilasi akan ditolak.</Notice>
          )}
          {view.progress && (
            <div className="text-sm" aria-label="Progres berbasis bukti">
              Progres (dari AcceptanceRecord, bukan estimasi): <strong>{view.progress.accepted_tasks}/{view.progress.total_tasks}</strong> task diterima ·{" "}
              {view.progress.criteria_with_trusted_evidence}/{view.progress.acceptance_criteria_total} acceptance criteria berbukti tepercaya
            </div>
          )}
          {(view.plan_warnings ?? []).length > 0 && (
            <Notice tone="warn">
              Temuan review (tidak memblokir DAG, ditinjau pemilik/reviewer):{" "}
              {(view.plan_warnings ?? []).map((w) => `${w.code}${w.key ? ` (${w.key})` : w.tasks ? ` (${w.tasks.join(", ")})` : ""}`).join("; ")}
            </Notice>
          )}
          {!view.nodes.length ? (
            <EmptyState title="Versi ini tidak memiliki task">{view.remedy ?? "Tambahkan TaskSpec melalui revisi rencana."}</EmptyState>
          ) : (
            <Tabs
              tabs={[
                {
                  id: "graph",
                  label: "Graf",
                  content: (
                    <Suspense fallback={<Loading label="Memuat graf…" />}>
                      <DagGraph view={view} highlight={highlight} />
                    </Suspense>
                  ),
                },
                { id: "table", label: "Tabel", content: <TaskTable view={view} highlight={highlight} /> },
                { id: "blockers", label: "Blocker & konflik", content: <BlockersAndConflicts view={view} /> },
              ]}
              initial={highlight ? "table" : "graph"}
            />
          )}
        </div>
      )}
    </div>
  );
}
