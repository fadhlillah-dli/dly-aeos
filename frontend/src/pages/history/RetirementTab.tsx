import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { Button, EmptyState, ErrorBox, inputClass, Loading, Mono, Notice } from "../../components/ui";
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

/** §33 final verification by the legitimate owner: a RETIREMENT decision bound to the checklist hash shown. */
export function RetirementDecisionForm({ data, isAdmin }: { data: RetirementChecklist; isAdmin: boolean }) {
  const qc = useQueryClient();
  const [rationale, setRationale] = useState("");
  const record = useMutation({
    mutationFn: () =>
      post(`/api/ops/retirement/${encodeURIComponent(data.project.id)}/decision`, { rationale, checklist_hash: data.checklist_hash ?? null }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ops-retirement", data.project.id] }),
  });
  if (data.project.lifecycle !== "RETIRING") {
    return <Notice tone="info">Verifikasi akhir dicatat saat proyek berstatus RETIRING; RETIRED hanya setelah keputusan RETIREMENT pemilik.</Notice>;
  }
  return (
    <div className="grid gap-2 rounded-md border border-line p-2 text-sm">
      <div className="font-medium">Verifikasi akhir pemilik (keputusan RETIREMENT)</div>
      <textarea
        aria-label="Rationale verifikasi akhir"
        className={inputClass}
        value={rationale}
        onChange={(e) => setRationale(e.target.value)}
        placeholder="Apa yang sudah Anda periksa (min. 10 karakter)"
      />
      <ErrorBox error={record.error} />
      <div>
        <Button disabled={!isAdmin || data.blocking.length > 0 || rationale.trim().length < 10} busy={record.isPending} onClick={() => record.mutate()}>
          Catat verifikasi akhir
        </Button>
      </div>
      <div className="text-xs text-ink-3">Terikat hash daftar periksa ini; perubahan setelahnya membuat verifikasi STALE. Tidak ada yang dihapus.</div>
    </div>
  );
}

export function RetirementTab() {
  const { projectId, isAdmin } = useAuth();
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
  return q.data ? (
    <div className="space-y-3">
      <RetirementView data={q.data} />
      <RetirementDecisionForm data={q.data} isAdmin={!!isAdmin} />
    </div>
  ) : null;
}
