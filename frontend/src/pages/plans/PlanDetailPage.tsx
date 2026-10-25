import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileDown, GitBranch, Pencil, Play, Radio } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { DecisionStatusBadge } from "../../components/planning/DecisionStatusBadge";
import { PlanStateBadge } from "../../components/planning/PlanStateBadge";
import type { PlanDetail, VersionView } from "../../components/planning/types";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Freshness, KeyValue, Loading, Mono, Notice, PageHeader, Table, Tabs } from "../../components/ui";
import { useEventStream } from "../../lib/events";
import { relativeTime, shortHash } from "../../lib/format";
import { type BridgeProof, BridgeProofNotice, SupervisorConfigNotice, type SupervisorUnevaluated } from "./BridgeProof";
import { ContentViewer } from "./ContentViewer";
import { ContextPanel, ReviewPanel } from "./ContextPanel";
import { DecisionPanel } from "./DecisionPanel";
import { EditVersionPanel } from "./EditVersionPanel";
import { StageTimeline } from "./StageTimeline";
import { VersionDiff, VersionHistory, VersionSelector } from "./VersionPanel";

function ManualFallbackCard({ plan }: { plan: PlanDetail }) {
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const imp = useMutation({
    mutationFn: () => post(`/api/plans/${plan.id}/manual-result`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["plan", plan.id] }),
  });
  const m = plan.manual;
  if (!m) return null;
  return (
    <Card title="Fallback manual: jalankan di Claude Code resmi" className="border-warn/40">
      <div className="grid gap-3 text-sm">
        <Notice tone="warn">
          Bridge Claude native belum terbukti/tersedia ({m.reason || "tidak diketahui"}). Tidak ada fallback ke Claude API. Tahap <Mono>{m.stage}</Mono> menunggu
          hasil manual.
        </Notice>
        <KeyValue items={[["Paket", <Mono key="p">{m.package_path}</Mono>], ["ID paket", <Mono key="i">{m.package_id}</Mono>]]} />
        <ol className="list-decimal pl-5">
          {m.steps.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ol>
        <p className="text-xs text-ink-3">{m.note}</p>
        <ErrorBox error={imp.error} />
        {isAdmin && (
          <div>
            <Button variant="primary" busy={imp.isPending} onClick={() => imp.mutate()}>
              <FileDown className="h-4 w-4" aria-hidden /> Impor hasil manual
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}

export default function PlanDetailPage() {
  const { planId = "" } = useParams();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [against, setAgainst] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const q = useQuery({ queryKey: ["plan", planId], queryFn: () => get<PlanDetail>(`/api/plans/${planId}`), refetchInterval: (query) => (query.state.data?.running ? 5000 : false) });
  const plan = q.data;
  const versionId = selected ?? plan?.current_version?.id ?? null;
  const version = useQuery({
    queryKey: ["plan", planId, "version", versionId],
    queryFn: () => get<VersionView>(`/api/plans/${planId}/versions/${versionId}`),
    enabled: !!versionId,
  });
  const { status: streamStatus, lastSeenAt } = useEventStream((e) => {
    const p = e.payload as { plan_id?: string };
    if (e.entity_id === planId || p.plan_id === planId || e.kind.startsWith("decision.")) qc.invalidateQueries({ queryKey: ["plan", planId] });
  });
  const run = useMutation({
    mutationFn: () => post(`/api/plans/${planId}/run`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["plan", planId] }),
  });

  if (q.isLoading) return <Loading />;
  if (q.error || !plan) return <ErrorBox error={q.error ?? new Error("Rencana tidak ditemukan")} />;
  const current = plan.current_version;
  const mode = current?.mode && current.mode !== "LIVE" ? current.mode : undefined;

  return (
    <div className="grid gap-4">
      <PageHeader
        title={plan.title}
        question="Apa isi versi rencana ini, apa yang berubah, dan keputusan apa yang diminta?"
        source="plans, plan_versions, planning_runs, planning_artifacts, decisions"
        checkedAt={plan.checked_at ?? null}
        evidenceLimit={plan.evidence_limit}
        mode={mode}
        actions={
          <>
            <Link to={`/dag?plan=${plan.id}`}>
              <Button>
                <GitBranch className="h-4 w-4" aria-hidden /> Task DAG
              </Button>
            </Link>
            {isAdmin && plan.allowed.run && (
              <Button variant="primary" busy={run.isPending || plan.running} onClick={() => run.mutate()}>
                <Play className="h-4 w-4" aria-hidden /> {plan.state === "CONTEXT_READY" ? "Jalankan perencanaan" : "Lanjutkan dari checkpoint"}
              </Button>
            )}
          </>
        }
      />
      <ErrorBox error={run.error} />
      {mode && <Notice tone="mock">Versi ini berasal dari output {mode}; bukan hasil model nyata dan tidak dihitung sebagai bukti.</Notice>}

      <Card>
        <div className="flex flex-wrap items-center gap-3">
          <PlanStateBadge state={plan.state} />
          {plan.running && <StatusBadge status="RUNNING" label="Pipeline berjalan" />}
          <span className="text-sm text-ink-2">{plan.state_reason}</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-4 text-xs text-ink-3">
          <span>
            Domain <Mono>{plan.domain}</Mono> · Proyek {plan.project_name ?? plan.project_id}
          </span>
          <span>
            Baseline: {(plan.policy_baseline.stages ?? []).map((s) => s.model).join(" → ")} · tahap tidak dapat dilewati
          </span>
          <span className="flex items-center gap-1">
            <Radio className="h-3 w-3" aria-hidden /> Stream {streamStatus}
          </span>
          <Freshness at={lastSeenAt ?? plan.checked_at ?? null} label="Pembaruan terakhir" staleAfterSeconds={120} />
        </div>
        <div className="mt-2">
          <BridgeProofNotice proof={(plan.checkpoint ?? {})["bridge_proof"] as BridgeProof | undefined} />
          <SupervisorConfigNotice unevaluated={(plan.checkpoint ?? {})["supervisor_unevaluated"] as SupervisorUnevaluated | undefined} />
        </div>
      </Card>

      <ManualFallbackCard plan={plan} />

      <section aria-label="Tahap perencanaan">
        <h2 className="mb-2 text-sm font-semibold">Empat tahap perencanaan (Opus → Fable → Opus → Fable)</h2>
        <StageTimeline stages={plan.stages} findings={plan.findings} verdict={plan.verdict} verdictEffective={plan.verdict_effective} />
      </section>

      {isAdmin && current && (plan.allowed.approve || plan.allowed.revise_or_reject) && (
        <DecisionPanel key={current.id} plan={plan} version={current} catalog={plan.operation_catalog} />
      )}
      {!isAdmin && plan.needs_decision && <Notice tone="info">Rencana ini menunggu keputusan SUPER_ADMIN. Peran VIEWER tidak dapat memutuskan.</Notice>}

      {editing && version.data && <EditVersionPanel planId={plan.id} base={version.data} onDone={() => setEditing(false)} />}

      <Card
        title="Isi rencana"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {plan.versions.length > 0 && (
              <div className="w-72">
                <VersionSelector
                  versions={plan.versions}
                  selected={versionId}
                  onSelect={(id) => setSelected(id)}
                  currentId={current?.id ?? null}
                  approvedId={plan.approved_version?.id ?? null}
                />
              </div>
            )}
            {isAdmin && plan.allowed.edit && version.data && version.data.id === current?.id && !editing && (
              <Button onClick={() => setEditing(true)}>
                <Pencil className="h-4 w-4" aria-hidden /> Edit sebagai versi baru
              </Button>
            )}
          </div>
        }
      >
        <Tabs
          tabs={[
            {
              id: "content",
              label: "Konten",
              content: !versionId ? (
                <EmptyState title="Belum ada versi rencana">Jalankan perencanaan untuk menghasilkan versi pertama.</EmptyState>
              ) : version.isLoading ? (
                <Loading />
              ) : version.error ? (
                <ErrorBox error={version.error} />
              ) : (
                <div className="grid gap-3">
                  <div className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
                    v{version.data!.version} · <Mono>{version.data!.manifest_hash}</Mono>
                    {version.data!.hash_valid ? <StatusBadge status="VERIFIED" label="Hash cocok dengan isi" /> : <StatusBadge status="FAILED" label="Hash tidak cocok" />}
                    {version.data!.is_approved && <StatusBadge status="APPROVED" />}
                  </div>
                  <ContentViewer content={version.data!.content} />
                </div>
              ),
            },
            { id: "context", label: "Konteks & pertanyaan", content: <ContextPanel context={plan.context} /> },
            {
              id: "review",
              label: "Temuan & verdict",
              content: <ReviewPanel findings={plan.findings} resolutions={plan.resolutions} verdict={plan.verdict} verdictEffective={plan.verdict_effective} />,
            },
            {
              id: "versions",
              label: "Versi & diff",
              content: (
                <div className="grid gap-4">
                  <VersionHistory versions={plan.versions} currentId={current?.id ?? null} approvedId={plan.approved_version?.id ?? null} />
                  {versionId && (
                    <div className="grid gap-2">
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        Bandingkan versi terpilih dengan
                        <select
                          aria-label="Bandingkan dengan"
                          className="rounded-md border border-line bg-surface px-2 py-1 text-sm"
                          value={against ?? ""}
                          onChange={(e) => setAgainst(e.target.value || null)}
                        >
                          <option value="">versi induk</option>
                          {plan.versions
                            .filter((v) => v.id !== versionId)
                            .map((v) => (
                              <option key={v.id} value={v.id}>
                                v{v.version} ({shortHash(v.manifest_hash)})
                              </option>
                            ))}
                        </select>
                      </div>
                      <VersionDiff planId={plan.id} versionId={versionId} against={against} />
                    </div>
                  )}
                </div>
              ),
            },
            {
              id: "decisions",
              label: "Riwayat keputusan",
              content: (
                <Table
                  rows={[...plan.decisions].reverse()}
                  rowKey={(d) => d.id}
                  empty="Belum ada keputusan untuk rencana ini"
                  columns={[
                    { header: "Jenis", cell: (d) => <Mono>{d.kind}</Mono> },
                    { header: "Status", cell: (d) => <DecisionStatusBadge status={d.status} /> },
                    { header: "Versi / hash", cell: (d) => <span>v{d.target_version} <Mono>{shortHash(d.target_hash)}</Mono></span> },
                    { header: "Operasi", cell: (d) => d.allowed_operations.join(", ") || "—" },
                    { header: "Oleh", cell: (d) => `${d.actor} (${d.actor_kind})` },
                    { header: "Kapan", cell: (d) => relativeTime(d.created_at) },
                    { header: "Alasan", cell: (d) => d.invalidation_reason ?? (d.rationale || "—") },
                  ]}
                />
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}
