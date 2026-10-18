import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { get } from "../../api/client";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { ErrorBox, Loading, Mono, Notice, Table } from "../../components/ui";
import { absoluteTime, relativeTime, shortHash } from "../../lib/format";
import type { ListMeta } from "./types";

const KINDS: { kind: string; label: string }[] = [
  { kind: "inventory", label: "Inventaris komponen" },
  { kind: "architecture", label: "Arsitektur & ADR" },
  { kind: "evidence", label: "Bukti" },
  { kind: "audit", label: "Audit" },
  { kind: "decisions", label: "Keputusan" },
];

interface SnapshotRow {
  snapshot_id: string;
  kind: string;
  generated_at: string;
  content_hash: string;
  item_count: number;
  redactions_applied: number;
}

export function ExportTab() {
  const qc = useQueryClient();
  // The download is a plain link (browser handles the file); refresh the registry shortly after so the
  // new snapshot (id/hash/time) is listed.
  const refreshSoon = () => window.setTimeout(() => qc.invalidateQueries({ queryKey: ["ops-exports"] }), 1500);
  const q = useQuery({ queryKey: ["ops-exports"], queryFn: () => get<ListMeta & { items: SnapshotRow[] }>("/api/ops/exports") });
  return (
    <div className="space-y-3">
      <Notice tone="info">
        Ekspor adalah <strong>snapshot</strong> (ID, hash, waktu, scope domain aktif) dan bukan status live. Isi diredaksi ulang sebelum keluar
        dari server; data domain lain tidak ikut.
      </Notice>
      <div className="flex flex-wrap gap-2">
        {KINDS.map((k) => (
          <a
            key={k.kind}
            href={`/api/ops/export?kind=${k.kind}&download=true`}
            onClick={refreshSoon}
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface px-3 py-1.5 text-sm hover:bg-surface-3"
          >
            <Download className="h-4 w-4" aria-hidden /> {k.label}
          </a>
        ))}
      </div>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : (
        <Table
          rows={q.data?.items ?? []}
          rowKey={(r) => r.snapshot_id}
          empty="Belum ada ekspor"
          columns={[
            { header: "Waktu", cell: (r) => <span title={absoluteTime(r.generated_at)}>{relativeTime(r.generated_at)}</span> },
            { header: "Jenis", cell: (r) => r.kind },
            { header: "Snapshot", cell: (r) => <Mono>{r.snapshot_id.slice(0, 8)}</Mono> },
            { header: "Hash", cell: (r) => <Mono>{shortHash(r.content_hash, 12)}</Mono> },
            { header: "Item", cell: (r) => String(r.item_count) },
            { header: "Redaksi", cell: (r) => (r.redactions_applied ? <OpsBadge status="RECORDED" label={`${r.redactions_applied} field diredaksi`} /> : "0") },
          ]}
        />
      )}
      <Provenance source={q.data?.source} checkedAt={q.data?.checked_at} evidenceLimit="Snapshot dibekukan pada waktu pembuatan; runtime dapat sudah berubah." />
    </div>
  );
}
