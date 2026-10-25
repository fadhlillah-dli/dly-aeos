import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { get } from "../../api/client";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { RetentionBadge } from "../../components/control_center/RetentionBadge";
import { Button, ErrorBox, Field, inputClass, Loading, Mono, Table } from "../../components/ui";
import { absoluteTime, relativeTime } from "../../lib/format";
import type { AuditRow, ListMeta } from "./types";

type Page = ListMeta & { items: AuditRow[]; next_before: number | null };

export function AuditTab() {
  const [action, setAction] = useState("");
  const [targetType, setTargetType] = useState("");
  const [result, setResult] = useState("");
  const [cursors, setCursors] = useState<number[]>([]);
  const before = cursors[cursors.length - 1];
  const params = new URLSearchParams({ limit: "50" });
  if (action) params.set("action", action);
  if (targetType) params.set("target_type", targetType);
  if (result) params.set("result", result);
  if (before) params.set("before", String(before));
  const q = useQuery({
    queryKey: ["history-audit", params.toString()],
    queryFn: () => get<Page>(`/api/control-center/history/audit?${params.toString()}`),
  });
  const reset = () => setCursors([]);
  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Aksi (awalan)">
          <input className={inputClass} value={action} placeholder="mis. ops. atau plan." onChange={(e) => { setAction(e.target.value); reset(); }} />
        </Field>
        <Field label="Jenis target">
          <input className={inputClass} value={targetType} placeholder="mis. backup" onChange={(e) => { setTargetType(e.target.value); reset(); }} />
        </Field>
        <Field label="Hasil">
          <select className={inputClass} value={result} onChange={(e) => { setResult(e.target.value); reset(); }}>
            <option value="">semua</option>
            <option value="SUCCESS">SUCCESS</option>
            <option value="DENIED">DENIED</option>
            <option value="FAILED">FAILED</option>
          </select>
        </Field>
      </div>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : (
        <Table
          rows={q.data?.items ?? []}
          rowKey={(r) => String(r.seq)}
          empty="Tidak ada entri audit untuk filter ini"
          columns={[
            { header: "Waktu", cell: (r) => <span title={absoluteTime(r.at)}>{relativeTime(r.at)}</span> },
            { header: "Aksi", cell: (r) => <Mono>{r.action}</Mono> },
            { header: "Target", cell: (r) => `${r.target_type} ${r.target_id ? r.target_id.slice(0, 16) : ""}` },
            { header: "Aktor", cell: (r) => `${r.actor_type}:${r.actor_id.slice(0, 18)}` },
            { header: "Hasil", cell: (r) => <OpsBadge status={r.result} /> },
            { header: "Alasan", cell: (r) => <span className="text-ink-3">{r.reason}</span> },
            { header: "Retensi", cell: (r) => <RetentionBadge marker={r.retention} /> },
          ]}
        />
      )}
      <div className="flex gap-2">
        <Button disabled={!cursors.length} onClick={() => setCursors(cursors.slice(0, -1))}>
          Lebih baru
        </Button>
        <Button disabled={!q.data?.next_before} onClick={() => q.data?.next_before && setCursors([...cursors, q.data.next_before])}>
          Lebih lama
        </Button>
      </div>
      <Provenance source={q.data?.source} checkedAt={q.data?.checked_at} evidenceLimit="Tabel append-only lokal; bukan jaminan WORM terhadap administrator host." />
    </div>
  );
}
