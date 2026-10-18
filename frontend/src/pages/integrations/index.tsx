import { useQuery } from "@tanstack/react-query";
import { get } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { Card, ErrorBox, Freshness, KeyValue, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { absoluteTime } from "../../lib/format";
import { CopyCommand } from "./CopyCommand";
import { IntegrationCard } from "./IntegrationCard";
import type { IntegrationsResponse, SecretStatusItem, SecretStatusResponse, StoreHealth } from "./types";

const STATE_BADGE: Record<string, { status: string; label: string }> = {
  PRESENT: { status: "CONFIGURED", label: "Tersimpan" },
  MISSING: { status: "MISSING", label: "Belum ada" },
  EXPIRED: { status: "EXPIRED", label: "Kedaluwarsa" },
  UNKNOWN: { status: "UNKNOWN", label: "Tidak diketahui" },
};

function StoreHealthCard({ h }: { h: StoreHealth }) {
  return (
    <Card title="Penyimpanan secret (OpenBao)" actions={<StatusBadge status={h.status} />}>
      <div className="space-y-3 text-sm">
        <KeyValue
          items={[
            ["Backend aktif", <Mono key="b">{h.backend}</Mono>],
            ["Alamat", <Mono key="a">{h.openbao.addr}</Mono>],
            ["Terjangkau", h.openbao.reachable ? "ya" : "tidak"],
            ["Terinisialisasi", h.openbao.initialized === null ? "UNKNOWN" : h.openbao.initialized ? "ya" : "belum"],
            ["Tersegel", h.openbao.sealed === null ? "UNKNOWN" : h.openbao.sealed ? "ya" : "tidak"],
            ["Versi", h.openbao.version ?? "UNKNOWN"],
            ["Login AppRole", h.openbao.login],
            ["AppRole di OS keyring", h.openbao.approle_in_keyring === null ? "UNKNOWN" : h.openbao.approle_in_keyring ? "ada" : "tidak ada"],
            ["Biner terpasang", h.openbao.binary_installed ? "ya" : "tidak"],
            ["OS keyring", `${h.keyring.available ? "tersedia" : "tidak tersedia"} (${h.keyring.platform_store})`],
            ["Diperiksa", <Freshness key="f" at={h.checked_at} />],
          ]}
        />
        <p className="text-xs text-ink-3">{h.evidence_limit}</p>
        {h.interim.active && h.interim.residual_risk && <Notice tone="warn">{h.interim.residual_risk}</Notice>}
        {h.reasons.length > 0 && (
          <ul className="list-disc pl-5 text-xs text-ink-2">
            {h.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        )}
        {h.remedies.length > 0 && (
          <div className="space-y-1.5">
            {h.remedies.map((r) => (
              <CopyCommand key={r.command} command={r.command} why={r.why} />
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}

export default function IntegrationsPage() {
  const integrations = useQuery({ queryKey: ["integrations"], queryFn: ({ signal }) => get<IntegrationsResponse>("/api/secrets/integrations", signal) });
  const status = useQuery({ queryKey: ["secrets-status"], queryFn: ({ signal }) => get<SecretStatusResponse>("/api/secrets/status", signal) });
  const health = useQuery({ queryKey: ["secrets-health"], queryFn: ({ signal }) => get<StoreHealth>("/api/secrets/health", signal) });

  const columns = [
    { header: "Referensi", cell: (s: SecretStatusItem) => <Mono>{s.ref}</Mono> },
    {
      header: "Status",
      cell: (s: SecretStatusItem) => {
        const b = STATE_BADGE[s.state] ?? { status: s.state, label: s.state };
        return <StatusBadge status={b.status} label={b.label} />;
      },
    },
    { header: "Backend", cell: (s: SecretStatusItem) => s.backend },
    { header: "Diperbarui", cell: (s: SecretStatusItem) => (s.updated_at ? absoluteTime(s.updated_at) : "—") },
    { header: "Kedaluwarsa", cell: (s: SecretStatusItem) => (s.expires_at ? absoluteTime(s.expires_at) : "—") },
    { header: "Dipakai oleh", cell: (s: SecretStatusItem) => <span className="text-xs">{s.purpose ?? "—"}</span> },
    { header: "Perbaikan", cell: (s: SecretStatusItem) => (s.state === "PRESENT" ? "—" : <CopyCommand command={s.remedy} />) },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Integrasi & Akses"
        question="Akun/target mana yang terhubung dan hak apa yang dimiliki?"
        source={integrations.data?.source ?? "status vault/keyring, uji bridge, QuotaState"}
        checkedAt={integrations.data?.checked_at ?? null}
        evidenceLimit="Kredensial tersimpan = terkonfigurasi, bukan terverifikasi; nilai secret tidak pernah ditampilkan"
      />
      <Notice tone="info">
        Nilai secret hanya dimasukkan lewat CLI lokal (mis. <Mono>aeos secrets set workers/openrouter</Mono>) dan tidak pernah
        tampil di dashboard, log, prompt model atau sandbox pekerja.
      </Notice>
      {integrations.isLoading && <Loading />}
      {integrations.error && <ErrorBox error={integrations.error} />}
      {integrations.data && (
        <>
          {integrations.data.office_hidden && (
            <p className="text-xs text-ink-3">Metadata kredensial OFFICE disembunyikan di sesi ini (pilih domain OFFICE untuk melihatnya).</p>
          )}
          <div className="grid gap-4 md:grid-cols-2">
            {integrations.data.items.map((c) => (
              <IntegrationCard key={c.id} card={c} />
            ))}
          </div>
        </>
      )}
      {health.data && <StoreHealthCard h={health.data} />}
      {health.error && <ErrorBox error={health.error} />}
      <Card title="Status secret (tanpa nilai)">
        {status.isLoading && <Loading />}
        {status.error && <ErrorBox error={status.error} />}
        {status.data && (
          <div className="space-y-3">
            {status.data.backend === "none" && (
              <Notice tone="warn">
                Vault tidak tersedia: status setiap referensi ditampilkan UNKNOWN (bukan "belum ada"). {status.data.reasons.join("; ")}
              </Notice>
            )}
            <Table rows={status.data.items} columns={columns} rowKey={(s) => s.ref} empty="Belum ada referensi secret" />
          </div>
        )}
      </Card>
    </div>
  );
}
