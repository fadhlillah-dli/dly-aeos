import { useQuery } from "@tanstack/react-query";
import { get } from "../../api/client";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { Card, EmptyState, ErrorBox, Loading, Mono, Table } from "../../components/ui";
import type { ListMeta, RunbookRow, ServiceRow } from "./types";

/** Service inventory (observed now; CONNECTED is not VERIFIED) and the runbook index (§33). */
export function ServicesView({ services, runbooks }: { services: ListMeta & { items: ServiceRow[] }; runbooks: { items: RunbookRow[] } }) {
  return (
    <div className="space-y-4">
      <Card title="Layanan lokal (diamati sekarang)">
        <Table
          rows={services.items}
          rowKey={(r) => r.service}
          empty="Tidak ada layanan tercatat"
          columns={[
            { header: "Layanan", cell: (r) => <Mono>{r.service}</Mono> },
            { header: "Jenis", cell: (r) => r.kind },
            { header: "Status", cell: (r) => <OpsBadge status={r.status} /> },
            { header: "Keterangan", cell: (r) => <span className="text-ink-3">{r.detail}</span> },
          ]}
        />
        <Provenance
          source={services.source}
          checkedAt={services.checked_at}
          evidenceLimit="Probe lokal: port terjangkau = CONNECTED, bukan VERIFIED. Verifikasi fungsi ada di Kesiapan (Doctor)."
        />
      </Card>
      <Card title="Runbook operasi">
        {runbooks.items.length === 0 ? (
          <EmptyState title="Runbook tidak ditemukan">
            Runbook ada di <Mono>docs/runbooks/</Mono> pada checkout repo; jalankan server dari repo atau rilis yang memuat folder itu.
          </EmptyState>
        ) : (
          <ul className="space-y-2 text-sm" aria-label="Runbook">
            {runbooks.items.map((r) => (
              <li key={r.id}>
                <div className="font-medium">{r.title}</div>
                <div className="text-xs text-ink-3">
                  Pemicu: {r.trigger || "—"} · <Mono>{r.path}</Mono>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

export function ServicesTab() {
  const services = useQuery({ queryKey: ["ops-services"], queryFn: () => get<ListMeta & { items: ServiceRow[] }>("/api/ops/services") });
  const runbooks = useQuery({ queryKey: ["ops-runbooks"], queryFn: () => get<{ items: RunbookRow[] }>("/api/ops/runbooks") });
  if (services.isLoading || runbooks.isLoading) return <Loading />;
  if (services.error) return <ErrorBox error={services.error} />;
  if (runbooks.error) return <ErrorBox error={runbooks.error} />;
  return <ServicesView services={services.data!} runbooks={runbooks.data!} />;
}
