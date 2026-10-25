import { useQuery } from "@tanstack/react-query";
import { get } from "../../api/client";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { Card, ErrorBox, Loading, Mono, Notice, Table } from "../../components/ui";
import type { ListMeta, RetentionRow } from "./types";

type RetentionList = ListMeta & {
  items: RetentionRow[];
  status: string;
  legal_hold_blocks_disposal: boolean;
  notice: string;
};

const CLASS_LABEL: Record<string, string> = {
  audit_events: "Audit event",
  evidence: "Evidence",
  attachments: "Lampiran",
  external_references: "Referensi eksternal",
  backups: "Backup",
};

/** The owner's retention rules (policy/retention.yaml) and how they apply to the stored records (§35). */
export function RetentionView({ data }: { data: RetentionList }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span>Status kebijakan:</span>
        <OpsBadge status={data.status} />
        {data.legal_hold_blocks_disposal && <span className="text-ink-3">Legal hold selalu memblokir disposal.</span>}
      </div>
      <Notice tone="info">{data.notice}</Notice>
      <Table
        rows={data.items}
        rowKey={(r) => r.data_class}
        columns={[
          { header: "Kelas data", cell: (r) => <span>{CLASS_LABEL[r.data_class] ?? r.data_class} <Mono>{r.data_class}</Mono></span> },
          { header: "Aturan", cell: (r) => <OpsBadge status={r.retain_days === null ? "NO_POLICY" : "KEEP"} label={r.retain_days === null ? undefined : `Simpan ${r.retain_days} hari`} /> },
          { header: "Keterangan", cell: (r) => <span className="text-ink-3">{r.label}</span> },
          { header: "Record tersimpan", cell: (r) => (r.evaluated ? String(r.records ?? 0) : <span className="text-ink-3">tidak dihitung di sini</span>) },
          {
            header: "Kedaluwarsa menurut kebijakan",
            cell: (r) =>
              !r.evaluated ? (
                <span className="text-ink-3">—</span>
              ) : r.expired_by_policy ? (
                <OpsBadge status="EXPIRED_BY_POLICY" label={`${r.expired_by_policy} record (disposal manual saja)`} />
              ) : (
                <span>0</span>
              ),
          },
        ]}
      />
    </div>
  );
}

export function RetentionTab() {
  const q = useQuery({ queryKey: ["ops-retention"], queryFn: () => get<RetentionList>("/api/ops/retention") });
  return (
    <Card title="Retensi audit & evidence">
      {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : q.data ? <RetentionView data={q.data} /> : null}
      <Provenance
        source={q.data?.source}
        checkedAt={q.data?.checked_at}
        evidenceLimit="AEOS tidak pernah menghapus data; durasi retensi adalah keputusan pemilik (OFFICE: kebijakan perusahaan)."
      />
    </Card>
  );
}
