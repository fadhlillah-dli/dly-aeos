import { StatusBadge } from "../../components/StatusBadge";
import { Card, Freshness, KeyValue, Mono, Notice } from "../../components/ui";
import { absoluteTime } from "../../lib/format";
import { CopyCommand } from "./CopyCommand";
import type { CredentialRef, IntegrationCardData } from "./types";

const REF_STATE: Record<string, { status: string; label: string }> = {
  PRESENT: { status: "CONFIGURED", label: "Tersimpan (belum diverifikasi fungsi)" },
  MISSING: { status: "MISSING", label: "Belum ada" },
  EXPIRED: { status: "EXPIRED", label: "Kedaluwarsa" },
  UNKNOWN: { status: "UNKNOWN", label: "Tidak diketahui (vault tidak tersedia)" },
  HIDDEN: { status: "NOT_APPLICABLE", label: "Disembunyikan di sesi ini" },
};

function RefRow({ r }: { r: CredentialRef }) {
  const spec = REF_STATE[r.state] ?? { status: r.state, label: r.state };
  return (
    <li className="flex flex-wrap items-center gap-2 text-sm">
      <Mono>{r.ref}</Mono>
      <StatusBadge status={spec.status} label={spec.label} />
      {r.updated_at && <span className="text-xs text-ink-3">diperbarui {absoluteTime(r.updated_at)}</span>}
      {r.expires_at && <span className="text-xs text-warn">kedaluwarsa {absoluteTime(r.expires_at)}</span>}
    </li>
  );
}

export function IntegrationCard({ card }: { card: IntegrationCardData }) {
  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-2">
          {card.name}
          {card.domain && <span className="rounded bg-surface-3 px-1.5 py-0.5 text-xs font-normal">{card.domain}</span>}
        </span>
      }
      actions={<StatusBadge status={card.status} />}
      className="h-full"
    >
      <div className="space-y-3 text-sm">
        <KeyValue
          items={[
            ["Kemampuan", card.capability],
            ["Cakupan", card.scope],
            ...(card.account
              ? ([["Akun", Object.entries(card.account).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join(" · ") || "UNKNOWN"]] as [string, string][])
              : []),
            ["Kedaluwarsa", card.expiry ? absoluteTime(card.expiry) : "—"],
            ["Bukti terakhir", card.proof_time ? <Freshness key="p" at={card.proof_time} label="Dibuktikan" staleAfterSeconds={7 * 86400} /> : <StatusBadge key="p" status="NOT_TESTED" label="Belum ada bukti" />],
            ["Sumber bukti", card.proof_source],
          ]}
        />
        {card.credential_refs.length > 0 && (
          <div>
            <div className="mb-1 font-medium">Referensi kredensial (tanpa nilai)</div>
            <ul className="space-y-1">
              {card.credential_refs.map((r) => (
                <RefRow key={r.ref} r={r} />
              ))}
            </ul>
          </div>
        )}
        {card.credential_note && <p className="text-xs text-ink-3">{card.credential_note}</p>}
        {card.quota && (
          <div className="text-xs">
            Kuota kredit: <StatusBadge status={card.quota.state === "OK" ? "CONNECTED" : card.quota.state === "EXHAUSTED" ? "BLOCKED" : "UNKNOWN"} label={card.quota.state} /> · kuota
            harian gratis: <StatusBadge status={card.quota.free_daily === "EXHAUSTED" ? "BLOCKED" : "UNKNOWN"} label={card.quota.free_daily} />
            {card.quota.observed_at && <> · <Freshness at={card.quota.observed_at} label="Diamati" /></>}
          </div>
        )}
        {card.blocked_reason && <Notice tone="warn">{card.blocked_reason}</Notice>}
        {card.warning && <Notice tone="warn">{card.warning}</Notice>}
        {card.remedies.length > 0 && (
          <div className="space-y-1.5">
            <div className="font-medium">Perintah perbaikan (dijalankan di terminal Anda)</div>
            {card.remedies.map((r) => (
              <CopyCommand key={r.command} command={r.command} why={r.why} />
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}
