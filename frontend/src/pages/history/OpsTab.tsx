import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { get, post, put } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { EvidenceChart, type EvidenceChartData } from "../../components/control_center/EvidenceChart";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, ErrorBox, inputBase, Loading, Mono, Table } from "../../components/ui";
import { relativeTime } from "../../lib/format";
import type { AlertRow, FlagRow, ListMeta, SliItem } from "./types";

function fmt(item: SliItem): string {
  if (item.value === null) return "UNKNOWN";
  if (item.unit === "ratio") return `${(item.value * 100).toFixed(1)}%`;
  if (item.unit === "usd") return `$${item.value.toFixed(4)}`;
  if (item.unit === "seconds") return item.value >= 3600 ? `${(item.value / 3600).toFixed(1)} jam` : `${Math.round(item.value)} dtk`;
  return String(item.value);
}

/** Integer flag editor; the backend validates type/range (e.g. interval >= 30 s) and audits the change. */
export function IntFlagEditor({ flag, disabled, onSave, busy }: { flag: FlagRow; disabled: boolean; onSave: (value: number) => void; busy: boolean }) {
  const [text, setText] = useState(String(flag.value ?? ""));
  const value = Number(text);
  const valid = text.trim() !== "" && Number.isInteger(value);
  return (
    <span className="inline-flex items-center gap-1">
      <input
        className={`${inputBase} w-24`}
        inputMode="numeric"
        value={text}
        disabled={disabled}
        aria-label={`Nilai ${flag.key}`}
        onChange={(e) => setText(e.target.value)}
      />
      <Button className="text-xs" disabled={disabled || !valid || value === flag.value} busy={busy} onClick={() => onSave(value)}>
        Simpan
      </Button>
    </span>
  );
}

export function OpsTab() {
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const sli = useQuery({ queryKey: ["ops-sli"], queryFn: () => get<ListMeta & { items: SliItem[]; targets_note: string }>("/api/ops/sli") });
  const alerts = useQuery({ queryKey: ["ops-alerts"], queryFn: () => get<ListMeta & { items: AlertRow[] }>("/api/ops/alerts") });
  const flags = useQuery({ queryKey: ["ops-flags"], queryFn: () => get<ListMeta & { items: FlagRow[]; notice: string }>("/api/ops/flags") });
  const evaluate = useMutation({
    mutationFn: () => post("/api/ops/sli/evaluate"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ops-alerts"] }),
  });
  const toggle = useMutation({
    mutationFn: (f: FlagRow) => put(`/api/ops/flags/${f.key}`, { value: !f.value, reason: "dashboard" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ops-flags"] }),
  });
  const setInt = useMutation({
    mutationFn: ({ key, value }: { key: string; value: number }) => put(`/api/ops/flags/${key}`, { value, reason: "dashboard" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ops-flags"] }),
  });
  // §29 evidence chart: the recorded samples of one SLI (bound to their record ids)
  const [chartKey, setChartKey] = useState<string | null>(null);
  const chart = useQuery({
    queryKey: ["ops-sli-chart", chartKey],
    queryFn: () => get<EvidenceChartData>(`/api/ops/sli/${chartKey}/chart`),
    enabled: chartKey !== null,
  });
  // §05 step 15: an alert (operations result) opens one incident that feeds the change process.
  const toIncident = useMutation({
    mutationFn: (alertId: string) => post<{ id: string; created: boolean }>(`/api/ops/alerts/${alertId}/incident`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ops-incidents"] }),
  });

  return (
    <div className="space-y-4">
      <Card
        title="SLI (target = keputusan pemilik)"
        actions={
          <Button className="text-xs" disabled={!isAdmin} busy={evaluate.isPending} onClick={() => evaluate.mutate()}>
            Evaluasi & perbarui alert
          </Button>
        }
      >
        {sli.isLoading ? (
          <Loading />
        ) : sli.error ? (
          <ErrorBox error={sli.error} />
        ) : (
          <>
            <Table
              rows={sli.data?.items ?? []}
              rowKey={(r) => r.key}
              columns={[
                { header: "Indikator", cell: (r) => <span title={r.key}>{r.title}</span> },
                { header: "Nilai", cell: (r) => (r.value === null ? <StatusBadge status="UNKNOWN" /> : fmt(r)) },
                { header: "Target", cell: (r) => (r.target ? `${r.target.max != null ? `≤ ${r.target.max}` : ""}${r.target.min != null ? ` ≥ ${r.target.min}` : ""}` : "—") },
                { header: "Status", cell: (r) => <OpsBadge status={r.status} /> },
                { header: "Scope", cell: (r) => r.scope },
                {
                  header: "Riwayat",
                  cell: (r) => (
                    <Button className="text-xs" onClick={() => setChartKey(chartKey === r.key ? null : r.key)}>
                      {chartKey === r.key ? "Tutup" : "Grafik"}
                    </Button>
                  ),
                },
              ]}
            />
            {chartKey && (
              <div className="mt-2 rounded-md border border-line p-2">
                {chart.isLoading ? <Loading /> : chart.error ? <ErrorBox error={chart.error} /> : chart.data ? <EvidenceChart data={chart.data} /> : null}
              </div>
            )}
            <p className="mt-2 text-xs text-ink-3">
              {sli.data?.targets_note} Contoh: <Mono>aeos ops sli target queue_age_seconds --max 1800</Mono>
            </p>
          </>
        )}
        {evaluate.error ? <ErrorBox error={evaluate.error} /> : null}
        <Provenance source={sli.data?.source} checkedAt={sli.data?.checked_at} evidenceLimit="Dihitung dari record controller; tanpa target pemilik tidak ada status 'dalam target'." />
      </Card>

      <Card title="Alert terbuka">
        {alerts.isLoading ? (
          <Loading />
        ) : alerts.error ? (
          <ErrorBox error={alerts.error} />
        ) : (
          <Table
            rows={alerts.data?.items ?? []}
            rowKey={(r) => r.id}
            empty="Tidak ada alert terbuka tercatat"
            columns={[
              { header: "Alert", cell: (r) => r.title },
              { header: "Tingkat", cell: (r) => <StatusBadge status={r.severity === "CRITICAL" || r.severity === "HIGH" ? "FAILED" : "DEGRADED"} label={r.severity} /> },
              { header: "Status", cell: (r) => <OpsBadge status={r.status} /> },
              { header: "Kejadian", cell: (r) => `${r.occurrences}× · ${relativeTime(r.last_seen_at)}` },
              { header: "Scope", cell: (r) => r.domain ?? "PLATFORM" },
              {
                header: "Tindak lanjut",
                cell: (r) => (
                  <Button
                    className="text-xs"
                    disabled={!isAdmin}
                    busy={toIncident.isPending && toIncident.variables === r.id}
                    onClick={() => toIncident.mutate(r.id)}
                  >
                    Buka insiden
                  </Button>
                ),
              },
            ]}
          />
        )}
        {toIncident.data ? (
          <p className="mt-2 text-xs text-ink-2">
            Insiden <Mono>{toIncident.data.id.slice(0, 8)}</Mono> {toIncident.data.created ? "dibuka" : "sudah ada"} untuk alert ini — lanjutkan di tab
            Insiden (analisis, regression/eval case, usulan perubahan).
          </p>
        ) : null}
        {toIncident.error ? <ErrorBox error={toIncident.error} /> : null}
        <Provenance source={alerts.data?.source} checkedAt={alerts.data?.checked_at} />
      </Card>

      <Card title="Feature flag">
        {flags.isLoading ? (
          <Loading />
        ) : flags.error ? (
          <ErrorBox error={flags.error} />
        ) : (
          <>
            <p className="mb-2 text-xs text-ink-3">{flags.data?.notice}</p>
            <Table
              rows={flags.data?.items ?? []}
              rowKey={(r) => r.key}
              columns={[
                { header: "Flag", cell: (r) => <Mono>{r.key}</Mono> },
                { header: "Fungsi", cell: (r) => r.description },
                { header: "Consumer", cell: (r) => r.consumer },
                { header: "Nilai", cell: (r) => `${String(r.value)}${r.is_default ? " (default)" : ""}` },
                {
                  header: "Ubah",
                  cell: (r) =>
                    r.kind === "bool" ? (
                      <Button className="text-xs" disabled={!isAdmin} busy={toggle.isPending && toggle.variables?.key === r.key} onClick={() => toggle.mutate(r)}>
                        {r.value ? "Nonaktifkan" : "Aktifkan"}
                      </Button>
                    ) : r.kind === "int" ? (
                      <IntFlagEditor
                        flag={r}
                        disabled={!isAdmin}
                        busy={setInt.isPending && setInt.variables?.key === r.key}
                        onSave={(value) => setInt.mutate({ key: r.key, value })}
                      />
                    ) : (
                      <span className="text-xs text-ink-3">via API (PUT /api/ops/flags/{r.key})</span>
                    ),
                },
              ]}
            />
          </>
        )}
        {toggle.error ? <ErrorBox error={toggle.error} /> : null}
        {setInt.error ? <ErrorBox error={setInt.error} /> : null}
      </Card>
    </div>
  );
}
