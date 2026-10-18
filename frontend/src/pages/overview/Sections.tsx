import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, KeyValue, Mono } from "../../components/ui";
import { relativeTime, shortHash, usd } from "../../lib/format";
import { SectionCard } from "./SectionCard";
import type { Overview } from "./types";

const CAPABILITY_LABELS: Record<string, string> = {
  CAN_OPEN_DASHBOARD: "Buka dashboard",
  CAN_PLAN_PERSONAL: "Perencanaan PERSONAL",
  CAN_PLAN_OFFICE: "Perencanaan OFFICE",
  CAN_PUBLISH_PERSONAL: "Publikasi PERSONAL",
  CAN_RUN_WORKER: "Jalankan worker",
  CAN_REVIEW_WITH_MAX: "Review dengan Claude Max",
  CAN_PREPARE_PR: "Siapkan PR",
  CAN_USE_OFFICE_INTEGRATIONS: "Integrasi OFFICE",
  CAN_VALIDATE_STAGING: "Validasi staging",
};

type S = Overview["sections"];

function MoreLink({ to, label }: { to: string; label: string }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1 text-sm text-accent hover:underline">
      {label} <ArrowRight className="h-4 w-4" aria-hidden />
    </Link>
  );
}

export function CapabilitiesCard({ section }: { section: S["capabilities"] }) {
  const caps = section.data?.items ?? [];
  return (
    <SectionCard title="Kesiapan capability" section={section} actions={<MoreLink to="/readiness" label="Detail & remedi" />}>
      {caps.length ? (
        <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3" aria-label="Kesiapan capability">
          {caps.map((c) => (
            <li key={c.capability} className="flex items-start justify-between gap-2 rounded-md border border-line p-2">
              <div className="min-w-0">
                <div className="text-sm font-medium">{CAPABILITY_LABELS[c.capability] ?? c.capability}</div>
                <div className="truncate text-xs text-ink-3" title={c.reasons.join("; ")}>
                  {c.reasons[0] ?? c.capability}
                </div>
              </div>
              <StatusBadge status={c.status} />
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState title="Status capability belum tersedia">
          Jalankan <Mono>aeos doctor</Mono>, lalu buka halaman Kesiapan.
        </EmptyState>
      )}
    </SectionCard>
  );
}

export function BlockersCard({ section }: { section: S["blockers"] }) {
  const items = section.data?.items ?? [];
  return (
    <SectionCard title="Blocker" section={section}>
      {items.length > 0 && (
        <ul className="space-y-1.5 text-sm">
          {items.map((b) => (
            <li key={`${b.kind}:${b.id}`} className="flex items-start gap-2">
              <StatusBadge status="BLOCKED" label={b.kind} />
              <Link to={b.link} className="min-w-0 flex-1 hover:underline">
                <span className="font-medium">{b.title}</span>
                {b.reason ? <span className="text-ink-3"> — {b.reason}</span> : null}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

export function DecisionsCard({ section }: { section: S["decisions"] }) {
  const top = section.data?.top ?? [];
  return (
    <SectionCard title="Keputusan tertunda" section={section} actions={<MoreLink to="/decisions" label="Antrean keputusan" />}>
      {top.length > 0 && (
        <ul className="space-y-1 text-sm">
          {top.map((d, i) => (
            <li key={String(d.id ?? i)}>
              <span className="font-medium">{String(d.title ?? d.kind ?? "Keputusan")}</span>
              {d.kind ? <span className="text-ink-3"> · {String(d.kind)}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

export function PlanPolicyCard({ plan, policy }: { plan: S["active_plan"]; policy: S["policy"] }) {
  const approved = plan.data?.approved ?? [];
  const awaiting = plan.data?.awaiting_review ?? [];
  return (
    <div className="space-y-4">
      <SectionCard title="Rencana aktif" section={plan}>
        {approved.map((p) => (
          <KeyValue
            key={p.plan_id}
            items={[
              ["Rencana", <Link to={`/plans/${p.plan_id}`} className="hover:underline">{p.title}</Link>],
              ["Versi disetujui", `v${p.version}`],
              ["Hash manifest", <Mono>{shortHash(p.manifest_hash, 16)}</Mono>],
            ]}
          />
        ))}
        {awaiting.length > 0 && <p className="mt-2 text-sm">Menunggu tinjauan: {awaiting.map((a) => a.title).join(", ")}</p>}
      </SectionCard>
      <SectionCard title="Snapshot kebijakan" section={policy}>
        {policy.data && (
          <KeyValue
            items={[
              ["Versi", policy.data.version],
              ["Hash", <Mono>{shortHash(policy.data.content_hash, 16)}</Mono>],
              ["MCI", <StatusBadge status={policy.data.mci_status} />],
            ]}
          />
        )}
      </SectionCard>
    </div>
  );
}

export function CostCard({ section }: { section: S["cost"] }) {
  const c = section.data;
  return (
    <SectionCard title="Biaya & kuota" section={section} actions={<MoreLink to="/costs" label="Detail biaya" />}>
      {c && (
        <>
          <KeyValue
            items={[
              [`Biaya tercatat (${c.window_days} hari)`, usd(c.actual_usd)],
              ["Estimasi (bukan aktual)", <span>{usd(c.estimated_usd)} <StatusBadge status="ESTIMATE" /></span>],
              ["Entri biaya UNKNOWN", c.unknown_cost_entries ? <StatusBadge status="UNKNOWN" label={`${c.unknown_cost_entries} entri`} /> : "0"],
              ["Entri MOCK dikecualikan", String(c.mock_entries_excluded)],
              ["Budget tercatat", String(c.budgets)],
            ]}
          />
          <ul className="mt-2 space-y-1 text-sm">
            {c.quotas.map((q) => (
              <li key={`${q.provider}:${q.scope}`} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{q.provider}</span>
                <span className="text-ink-3">{q.scope}</span>
                <StatusBadge status={q.state === "OK" ? "CONFIGURED" : q.state} label={`Kuota: ${q.state}`} />
                {q.note ? <span className="text-xs text-ink-3">{q.note}</span> : null}
              </li>
            ))}
          </ul>
        </>
      )}
    </SectionCard>
  );
}

export function HealthCard({ health, recovery, secrets, stream }: { health: S["health"]; recovery: S["recovery"]; secrets: S["secrets"]; stream: S["stream"] }) {
  const subs = health.data?.items ?? [];
  return (
    <SectionCard title="Kesehatan" section={health} actions={<MoreLink to="/history" label="Riwayat & pemulihan" />}>
      <ul className="space-y-1 text-sm">
        {subs.map((h) => (
          <li key={h.key} className="flex flex-wrap items-center gap-2">
            <OpsBadge status={h.status} />
            <span className="font-medium">{h.key}</span>
            <span className="text-ink-3">{h.detail}</span>
          </li>
        ))}
        <li className="flex flex-wrap items-center gap-2">
          <OpsBadge status={recovery.status} />
          <span className="font-medium">pemulihan</span>
          <span className="text-ink-3">{recovery.reason}</span>
        </li>
        <li className="flex flex-wrap items-center gap-2">
          <OpsBadge status={secrets.status} />
          <span className="font-medium">secret</span>
          <span className="text-ink-3">{secrets.reason || "status saja, tanpa nilai"}</span>
        </li>
        <li className="flex flex-wrap items-center gap-2">
          <OpsBadge status={stream.status} />
          <span className="font-medium">event</span>
          <span className="text-ink-3">
            {stream.data?.last_event_at ? `terakhir ${relativeTime(stream.data.last_event_at)} (#${stream.data.latest_seq})` : stream.reason}
          </span>
        </li>
      </ul>
    </SectionCard>
  );
}
