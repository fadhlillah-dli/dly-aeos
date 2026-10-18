import { useQuery } from "@tanstack/react-query";
import { get } from "../../api/client";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, ErrorBox, Loading, Mono, Table } from "../../components/ui";
import { absoluteTime, relativeTime, shortHash } from "../../lib/format";
import type { DecisionRow, ListMeta, PublicationRow, SnapshotRow } from "./types";

function useList<T>(key: string, url: string) {
  return useQuery({ queryKey: [key], queryFn: () => get<ListMeta & { items: T[] }>(url) });
}

export function DecisionsTab() {
  const q = useList<DecisionRow>("history-decisions", "/api/control-center/history/decisions");
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  return (
    <div>
      <Table
        rows={q.data?.items ?? []}
        rowKey={(r) => r.id}
        empty="Belum ada keputusan tercatat di domain ini"
        columns={[
          { header: "Waktu", cell: (r) => <span title={absoluteTime(r.created_at)}>{relativeTime(r.created_at)}</span> },
          { header: "Jenis", cell: (r) => <Mono>{r.kind}</Mono> },
          { header: "Target", cell: (r) => `${r.target_type} ${r.target_id.slice(0, 20)}${r.target_version ? ` v${r.target_version}` : ""}` },
          { header: "Hash", cell: (r) => <Mono>{shortHash(r.target_hash)}</Mono> },
          { header: "Oleh", cell: (r) => r.actor },
          { header: "Status", cell: (r) => <StatusBadge status={r.status === "ACTIVE" ? "APPROVED" : r.status === "INVALIDATED" ? "STALE" : r.status} label={r.status} /> },
          { header: "Alasan", cell: (r) => <span className="text-ink-3">{r.invalidation_reason ?? r.rationale}</span> },
        ]}
      />
      <Provenance source={q.data?.source} checkedAt={q.data?.checked_at} evidenceLimit="Keputusan manusia terikat target + hash; persetujuan lama tidak berlaku setelah hash berubah." />
    </div>
  );
}

function render(v: unknown): string {
  if (v === undefined) return "—";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > 80 ? `${s.slice(0, 80)}…` : s;
}

export function SnapshotsTab() {
  const q = useList<SnapshotRow>("history-snapshots", "/api/control-center/history/snapshots");
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const items = q.data?.items ?? [];
  return (
    <div className="space-y-3">
      {items.length === 0 && (
        <EmptyState title="Belum ada snapshot arsitektur/konfigurasi">
          Snapshot dibuat saat rancangan/manifest disetujui (modul policy/inventory). Jalankan <Mono>aeos seed</Mono> untuk katalog desain.
        </EmptyState>
      )}
      {items.map((s) => (
        <div key={s.id} className="rounded-md border border-line p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">{s.label || s.id.slice(0, 8)}</span>
            <Mono>{shortHash(s.snapshot_hash, 12)}</Mono>
            <span className="text-ink-3" title={absoluteTime(s.created_at)}>{relativeTime(s.created_at)}</span>
          </div>
          {s.previous_id ? (
            s.diff.length ? (
              <ul className="mt-2 space-y-0.5 text-xs">
                {s.diff.map((d) => (
                  <li key={d.path + d.change} className="flex flex-wrap gap-2">
                    <StatusBadge status={d.change === "added" ? "CONFIGURED" : d.change === "removed" ? "CANCELLED" : "DRIFTED"} label={d.change} />
                    <Mono>{d.path}</Mono>
                    <span className="text-ink-3">{render(d.before)} → {render(d.after)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-xs text-ink-3">Tidak ada perubahan dibanding snapshot sebelumnya.</p>
            )
          ) : (
            <p className="mt-1 text-xs text-ink-3">Snapshot pertama (tidak ada pembanding).</p>
          )}
        </div>
      ))}
      <Provenance source={q.data?.source} checkedAt={q.data?.checked_at} evidenceLimit={q.data?.evidence_limit} />
    </div>
  );
}

export function PublicationsTab() {
  const q = useList<PublicationRow>("history-publications", "/api/control-center/history/publications");
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  return (
    <div>
      <Table
        rows={q.data?.items ?? []}
        rowKey={(r) => r.id}
        empty="Belum ada publikasi di domain ini"
        columns={[
          { header: "Waktu", cell: (r) => <span title={absoluteTime(r.created_at)}>{relativeTime(r.created_at)}</span> },
          { header: "Status", cell: (r) => <OpsBadge status={r.status} /> },
          { header: "Hash disetujui", cell: (r) => <Mono>{shortHash(r.approved_hash)}</Mono> },
          { header: "Target", cell: (r) => String(r.targets) },
          {
            header: "Operasi",
            cell: (r) => (
              <span className="flex flex-wrap gap-1">
                {Object.entries(r.operations).map(([st, n]) => (
                  <StatusBadge key={st} status={st} label={`${st} ${n}`} />
                ))}
              </span>
            ),
          },
          { header: "Mode", cell: (r) => <StatusBadge status={r.mode} /> },
        ]}
      />
      <Provenance source={q.data?.source} checkedAt={q.data?.checked_at} evidenceLimit={q.data?.evidence_limit} />
    </div>
  );
}
