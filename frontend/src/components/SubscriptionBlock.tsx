import { MappedBadge, SUBSCRIPTION_STATE } from "./proposals/MappedBadge";
import { StatusBadge } from "./StatusBadge";
import { KeyValue, Mono, Notice, Table } from "./ui";
import { usd } from "../lib/format";

/** ADR-0080 amendment (2026-10-26): allowance consumed per provider window of a flat-fee SUBSCRIPTION route
 * (backend: aeos.cost.subscription.allowance_status). The figures are allowance, never per-token spend. */
export interface SubscriptionWindow {
  window: "5h" | "week" | "month" | string;
  share: number;
  trailing_hours: number;
  since: string;
  /** only the 5h window blocks admission; week and month are advisory */
  blocking: boolean;
  limit_usd: number;
  used_usd: number;
  remaining_usd: number;
  reported_usd: number;
  estimated_usd: number;
  requests: number;
  unknown_requests: number;
  state: string;
  blocked_until?: string;
}

export interface SubscriptionStatus {
  route_key: string;
  provider: string;
  status: string;
  billing: "SUBSCRIPTION";
  plan: string | null;
  currency: string;
  monthly_fee_usd: number | null;
  allowance_usd_month: number | null;
  basis: string;
  counted_scope: string;
  checked_at: string;
  /** OK | WARN_EXHAUSTED | EXHAUSTED | UNKNOWN | BEYOND_PLAN | UNTRUSTED */
  state: string;
  /** null (with spend_hidden) when the allowance spans domains the principal cannot see */
  windows: SubscriptionWindow[] | null;
  spend_hidden?: boolean;
  spend_note?: string;
  problems?: string[];
  warnings?: string[];
  beyond_plan?: { entry_id: string; at: string | null; amount_usd: string; billing_source: string | null; activated_at: string | null; remedy: string } | null;
  beyond_plan_usd?: number;
  beyond_plan_unknown_requests?: number;
}

const WINDOW_LABEL: Record<string, string> = { "5h": "5 jam", week: "Minggu", month: "Bulan" };

/** Plan, allowance per window and the beyond-plan / Extra Usage state of one subscription route. */
export function SubscriptionStatusBlock({ s, showRoute = false }: { s: SubscriptionStatus; showRoute?: boolean }) {
  const unknown = s.beyond_plan_unknown_requests ?? 0;
  return (
    <div className="space-y-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {showRoute && <Mono>{s.route_key}</Mono>}
        <MappedBadge value={s.state} map={SUBSCRIPTION_STATE} />
        <StatusBadge status="NOT_APPLICABLE" label="Langganan — bukan biaya per token" />
        <StatusBadge status={s.status} />
      </div>
      <KeyValue
        items={[
          ["Paket", `${s.plan ?? "langganan"} · ${s.monthly_fee_usd === null ? "biaya UNKNOWN" : usd(s.monthly_fee_usd)}/bulan`],
          ["Allowance per bulan", s.allowance_usd_month === null ? "UNKNOWN" : usd(s.allowance_usd_month)],
        ]}
      />
      {s.spend_hidden ? (
        <p className="text-xs text-ink-3">Allowance disembunyikan: {s.spend_note}</p>
      ) : (
        <Table
          rows={s.windows ?? []}
          rowKey={(w) => w.window}
          empty="Belum ada jendela allowance"
          columns={[
            { header: "Jendela", cell: (w) => <span>{WINDOW_LABEL[w.window] ?? w.window}{w.blocking ? "" : " (saran)"}</span> },
            { header: "Terpakai", cell: (w) => usd(w.used_usd) },
            { header: "Batas", cell: (w) => usd(w.limit_usd) },
            { header: "Sisa", cell: (w) => usd(w.remaining_usd) },
            { header: "Dilaporkan / estimasi", cell: (w) => `${usd(w.reported_usd)} / ${usd(w.estimated_usd)}` },
            { header: "Request", cell: (w) => w.requests },
            { header: "Status", cell: (w) => <MappedBadge value={w.state} map={SUBSCRIPTION_STATE} /> },
          ]}
        />
      )}
      {((s.beyond_plan_usd ?? 0) > 0 || unknown > 0) && (
        <Notice tone="bad">
          Dibayar di luar paket: {usd(s.beyond_plan_usd ?? 0)}
          {unknown > 0 ? ` + ${unknown} panggilan berjumlah UNKNOWN` : ""} (fallback berbayar di sisi provider terpakai).
        </Notice>
      )}
      {s.beyond_plan && (
        <Notice tone="bad">
          Ditolak (SUBSCRIPTION_BEYOND_PLAN) sejak {s.beyond_plan.at ?? "UNKNOWN"}: {s.beyond_plan.remedy}
        </Notice>
      )}
      {(s.problems ?? []).map((p) => (
        <Notice key={p} tone="warn">
          Memblokir: {p}
        </Notice>
      ))}
      {(s.warnings ?? []).map((w) => (
        <Notice key={w} tone="info">
          Saran: {w}
        </Notice>
      ))}
      <p className="text-xs text-ink-3">
        Dihitung dari panggilan lewat instance AEOS ini saja; batas provider sendiri tetap otoritatif. Jendela 5 jam memblokir admission; minggu dan
        bulan hanya peringatan.
      </p>
    </div>
  );
}
