import { Building2, FlaskConical, ShieldAlert, User } from "lucide-react";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { StatusBadge } from "../../components/StatusBadge";
import { Card, Mono, Notice } from "../../components/ui";
import { absoluteTime, relativeTime } from "../../lib/format";
import { BlockersCard, CapabilitiesCard, CostCard, DecisionsCard, HealthCard, PlanPolicyCard } from "./Sections";
import type { Overview } from "./types";

export function ModeBanner({ data }: { data: Overview }) {
  if (data.mode === "LIVE" && !data.contains_mock) return null;
  const rows = Object.entries(data.mock_rows ?? {})
    .filter(([, n]) => n > 0)
    .map(([t, n]) => `${t}: ${n}`)
    .join(", ");
  return (
    <Notice tone="mock">
      <span className="inline-flex items-center gap-1 font-medium">
        <FlaskConical className="h-4 w-4" aria-hidden /> Data MOCK/DEMO terdeteksi ({data.mode})
      </span>
      <div>Baris fixture tidak dihitung sebagai hasil, biaya atau workforce live{rows ? ` (${rows})` : ""}.</div>
    </Notice>
  );
}

function ContextHeader({ data }: { data: Overview }) {
  const hint = data.sections.project_hint;
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3">
        <span className="inline-flex items-center gap-1 rounded-md bg-surface-3 px-2 py-1 text-sm font-semibold">
          {data.domain === "OFFICE" ? <Building2 className="h-4 w-4" aria-hidden /> : <User className="h-4 w-4" aria-hidden />}
          {data.domain}
        </span>
        {data.project ? (
          <>
            <span className="text-lg font-semibold">{data.project.name}</span>
            <Mono>{data.project.project_key}</Mono>
            {data.project.is_platform && <StatusBadge status="CONFIGURED" label="Proyek platform (self-build)" />}
            <StatusBadge status={data.project.mode} />
          </>
        ) : (
          <span className="text-ink-2">{hint?.reason ?? "Belum memilih proyek."}</span>
        )}
        <span className="ml-auto text-xs text-ink-3" title={absoluteTime(data.generated_at)}>
          Diperiksa {relativeTime(data.generated_at)}
        </span>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs text-ink-3">
        <span>Status per bagian (tidak ada satu tanda hijau untuk seluruh jaminan):</span>
        {Object.entries(data.status_counts).map(([st, n]) => (
          <span key={st} className="inline-flex items-center gap-1">
            <OpsBadge status={st} /> ×{n}
          </span>
        ))}
      </div>
    </Card>
  );
}

export function OverviewView({ data }: { data: Overview }) {
  const s = data.sections;
  const emergency = s.emergency.status === "RECORDED" ? s.emergency.data : null;
  return (
    <div className="space-y-4">
      <ModeBanner data={data} />
      {emergency && (
        <Notice tone="warn">
          <span className="inline-flex items-center gap-1 font-medium">
            <ShieldAlert className="h-4 w-4" aria-hidden /> Emergency stop terakhir: {emergency.status}
          </span>{" "}
          {relativeTime(emergency.at)} ({emergency.reason}). Lanjutkan penjadwalan secara eksplisit bila aman.
        </Notice>
      )}
      <ContextHeader data={data} />
      <CapabilitiesCard section={s.capabilities} />
      <div className="grid gap-4 lg:grid-cols-2">
        <BlockersCard section={s.blockers} />
        <DecisionsCard section={s.decisions} />
        <PlanPolicyCard plan={s.active_plan} policy={s.policy} />
        <CostCard section={s.cost} />
      </div>
      <HealthCard health={s.health} recovery={s.recovery} secrets={s.secrets} stream={s.stream} />
    </div>
  );
}
