import { useQuery } from "@tanstack/react-query";
import { get } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { Card, ErrorBox, Freshness, KeyValue, Loading, Mono, Notice, Table } from "../../components/ui";
import { label } from "./labels";
import type { Connections, ListResponse, TargetView } from "./types";

export function ConnectionsPanel({ projectId }: { projectId: string }) {
  const conn = useQuery({ queryKey: ["publisher-connections"], queryFn: () => get<Connections>("/api/publisher/connections") });
  const targets = useQuery({
    queryKey: ["publication-targets", projectId],
    queryFn: () => get<ListResponse<TargetView> & { note: string }>(`/api/projects/${projectId}/publication-targets`),
  });
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Target publikasi proyek" actions={<Freshness at={targets.data?.checked_at} />}>
        {targets.isLoading ? (
          <Loading />
        ) : targets.error ? (
          <ErrorBox error={targets.error} />
        ) : (
          <div className="space-y-3">
            <Table
              rows={targets.data!.items}
              rowKey={(t) => t.family}
              columns={[
                { header: "Tujuan", cell: (t) => (t.purpose === "docs" ? "Dokumen" : "Work item") },
                { header: "Provider", cell: (t) => t.label },
                { header: "Status", cell: (t) => <StatusBadge status={t.status === "READY" ? "CONFIGURED" : t.status} label={t.status === "READY" ? "Terkonfigurasi (belum diverifikasi)" : label(t.status)} /> },
                {
                  header: "Alasan / remedy",
                  cell: (t) => (
                    <div className="space-y-1 text-xs">
                      {t.reasons.map((r) => (
                        <div key={r}>{r}</div>
                      ))}
                      {t.remedies.map((r) => (
                        <div key={r}>
                          <Mono>{r}</Mono>
                        </div>
                      ))}
                    </div>
                  ),
                },
              ]}
            />
            <Notice tone="info">{targets.data!.note}</Notice>
          </div>
        )}
      </Card>
      <Card title="Koneksi akun" actions={<Freshness at={conn.data?.checked_at} />}>
        {conn.isLoading ? (
          <Loading />
        ) : conn.error ? (
          <ErrorBox error={conn.error} />
        ) : (
          <div className="space-y-4 text-sm">
            {conn.data!.google && (
              <div className="space-y-2">
                <div className="font-medium">Google Drive / Docs</div>
                <KeyValue
                  items={[
                    ["Status", <StatusBadge key="s" status={conn.data!.google.status} label={label(conn.data!.google.status)} />],
                    ["Scope diminta", <Mono key="r">{conn.data!.google.requested_scopes.join(", ")}</Mono>],
                    ["Scope token", conn.data!.google.granted_scopes?.length ? <Mono key="g">{conn.data!.google.granted_scopes.join(", ")}</Mono> : "—"],
                  ]}
                />
                {conn.data!.google.status !== "CONFIGURED" && (
                  <p className="text-xs">
                    Hubungkan: <Mono>{conn.data!.google.remedy}</Mono>
                  </p>
                )}
                <p className="text-xs text-ink-3">
                  Hanya scope least-privilege drive.file. Gambar disematkan dalam snapshot DOCX; tidak ada link publik.
                </p>
              </div>
            )}
            {conn.data!.github && (
              <div className="space-y-2">
                <div className="font-medium">GitHub Issues</div>
                <StatusBadge status={conn.data!.github.status} label={label(conn.data!.github.status)} />
                {conn.data!.github.status !== "CONFIGURED" && (
                  <p className="text-xs">
                    Simpan token: <Mono>{conn.data!.github.remedy}</Mono>
                  </p>
                )}
              </div>
            )}
            {conn.data!.atlassian && (
              <Notice tone="warn">
                {conn.data!.atlassian.reason}. Data Center: tidak didukung adapter ini. Cloud: hanya diuji dengan HTTP tiruan
                (NOT-PROVEN pada tenant nyata).
              </Notice>
            )}
            <p className="text-xs text-ink-3">Nilai secret tidak pernah ditampilkan; status CONFIGURED bukan VERIFIED.</p>
          </div>
        )}
      </Card>
    </div>
  );
}
