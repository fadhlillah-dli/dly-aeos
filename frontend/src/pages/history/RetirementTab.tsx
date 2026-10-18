import { useQuery } from "@tanstack/react-query";
import { get } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { EmptyState, ErrorBox, Loading, Mono, Notice } from "../../components/ui";
import type { RetirementChecklist } from "./types";

function detailText(detail: Record<string, unknown>): string {
  return Object.entries(detail)
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? (v.length ? v.join(", ") : "—") : v === null || v === undefined ? "—" : String(v)}`)
    .join(" · ");
}

/** Read-only retirement checklist (§33): maps what exists and which owner actions remain. */
export function RetirementView({ data }: { data: RetirementChecklist }) {
  return (
    <div className="space-y-3">
      <Notice tone="info">{data.notice}</Notice>
      {data.blocking.length > 0 && (
        <Notice tone="warn">
          Masih memblokir: {data.blocking.join(", ")}. Selesaikan butir ini sebelum keputusan RETIREMENT dicatat pemilik.
        </Notice>
      )}
      <ol className="space-y-2 text-sm" aria-label="Daftar periksa retirement">
        {data.items.map((i) => (
          <li key={i.key} className="rounded-md border border-line p-2">
            <div className="flex flex-wrap items-center gap-2">
              <OpsBadge status={i.status} />
              <span className="font-medium">{i.title}</span>
            </div>
            <div className="mt-1 text-ink-2">{i.owner_action}</div>
            {Object.keys(i.detail).length > 0 && <div className="mt-0.5 text-xs text-ink-3">{detailText(i.detail)}</div>}
          </li>
        ))}
      </ol>
      <Provenance
        source="projects, task_executions, capability_grants, publications, integrations, backups (read-only)"
        checkedAt={data.checked_at}
        evidenceLimit="Daftar periksa, bukan bukti penutupan; tidak ada yang dihapus atau dicabut dari halaman ini."
      />
    </div>
  );
}

export function RetirementTab() {
  const { projectId } = useAuth();
  const q = useQuery({
    queryKey: ["ops-retirement", projectId],
    queryFn: () => get<RetirementChecklist>(`/api/ops/retirement/${encodeURIComponent(projectId ?? "")}`),
    enabled: !!projectId,
  });
  if (!projectId) {
    return (
      <EmptyState title="Belum memilih proyek">
        Pilih proyek di header untuk melihat daftar periksa retirement-nya (API: <Mono>GET /api/ops/retirement/&lt;project_id&gt;</Mono>).
      </EmptyState>
    );
  }
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  return q.data ? <RetirementView data={q.data} /> : null;
}
