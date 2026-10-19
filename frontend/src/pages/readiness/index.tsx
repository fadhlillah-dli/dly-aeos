import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import { AlertTriangle, ChevronDown, ChevronRight, ExternalLink, Play, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { RemedyPanel } from "../../components/doctor/RemedyPanel";
import { isOk } from "../../components/doctor/status";
import type { CapabilitiesResponse, CapabilityItem, CheckItem, ChecksResponse, HistoryResponse } from "../../components/doctor/types";
import { Badge, SeverityBadge } from "../../components/inventory/labels";
import { Button, Card, EmptyState, ErrorBox, Freshness, KeyValue, Loading, Mono, Notice, PageHeader } from "../../components/ui";
import { useEventStream } from "../../lib/events";
import { absoluteTime, relativeTime } from "../../lib/format";

/** Additive fields of the capability matrix (integration 2026-10-18): residual risks that stay true even when a
 * gate is READY (WEAK process sandbox, branch protection NOT_ENFORCEABLE) and live gate entries. */
type Capability = Omit<CapabilityItem, "checks"> & {
  residual_risks?: string[];
  checks: (CapabilityItem["checks"][number] & { live?: boolean })[];
};

/** Additive field of /api/doctor/checks: a failed Doctor pass (e.g. at server start) newer than the last run. */
type ProjectCheckItem = { check_id: string; title: string; project_id: string; effective_status: string; summary: string; last_checked_at: string | null };
type ClaudeReviewData = {
  at: string | null;
  ok: boolean;
  summary: string | null;
  findings: { check_id: string; observation: string; suggestion: string }[];
  note: string;
  error_code: string | null;
  model_resolved: string | null;
};
type ChecksData = ChecksResponse & {
  last_failure?: { at: string; trigger: string | null; error: string | null } | null;
  project_items?: ProjectCheckItem[];
  claude_review?: ClaudeReviewData | null;
};

/** How a Doctor run was started (``DoctorRun.trigger``). */
const TRIGGER_LABELS: Record<string, string> = {
  startup: "otomatis saat server mulai",
  api: "dashboard",
  cli: "CLI (aeos doctor)",
  recheck: "otomatis: cek ulang setelah perubahan",
};

function triggerLabel(trigger: string | null | undefined): string {
  if (!trigger) return "tidak diketahui";
  return TRIGGER_LABELS[trigger] ?? trigger;
}

function ResidualRisks({ risks }: { risks: string[] }) {
  const [open, setOpen] = useState(false);
  if (!risks.length) return null;
  const shown = open ? risks : risks.slice(0, 1);
  return (
    <div className="mt-2 rounded-md bg-warn-bg p-2 text-xs text-warn" data-testid="residual-risks">
      <div className="mb-0.5 flex items-center gap-1 font-semibold">
        <AlertTriangle className="h-3.5 w-3.5" aria-hidden /> Risiko residual ({risks.length}) — tetap berlaku walau READY, bukan
        tanda lulus
      </div>
      <ul className="list-disc space-y-0.5 pl-4">
        {shown.map((r, i) => (
          <li key={i} className="break-words">
            {r}
          </li>
        ))}
      </ul>
      {risks.length > 1 && (
        <button className="mt-1 hover:underline" onClick={() => setOpen(!open)}>
          {open ? "Ringkas" : `Lihat semua risiko (${risks.length})`}
        </button>
      )}
    </div>
  );
}

function CapabilityRow({ cap }: { cap: Capability }) {
  const [open, setOpen] = useState(false);
  const reasons = open ? cap.reasons : cap.reasons.slice(0, 2);
  const live = cap.checks.filter((c) => c.live);
  return (
    <tr className="align-top">
      <td className="px-3 py-2">
        <div className="font-medium">{cap.label}</div>
        <div className="mono text-xs text-ink-3">{cap.capability}</div>
        {cap.domain && <div className="text-xs text-ink-3">Domain: {cap.domain}</div>}
        {live.map((c) => (
          <div key={c.check_id} className="mt-1 text-xs text-ink-3" title="Dievaluasi langsung setiap kali gate dibaca">
            {c.title}: <Badge status={c.effective_status} />
          </div>
        ))}
      </td>
      <td className="px-3 py-2">
        <Badge status={cap.status} />
      </td>
      <td className="px-3 py-2 text-sm">
        {cap.reasons.length === 0 ? (
          <span className="text-ink-3">Semua pemeriksaan kritis dan wajib OK.</span>
        ) : (
          <ul className="list-disc space-y-0.5 pl-4">
            {reasons.map((r, i) => (
              <li key={i} className="break-words">
                {r}
              </li>
            ))}
          </ul>
        )}
        {cap.reasons.length > 2 && (
          <button className="mt-1 text-xs text-accent hover:underline" onClick={() => setOpen(!open)}>
            {open ? "Ringkas" : `Lihat semua (${cap.reasons.length})`}
          </button>
        )}
        <ResidualRisks risks={cap.residual_risks ?? []} />
      </td>
      <td className="px-3 py-2 text-xs text-ink-3">{cap.remedies.length ? `${cap.remedies.length} usulan perbaikan` : "—"}</td>
    </tr>
  );
}

function CheckDetail({ check, onClose }: { check: CheckItem; onClose: () => void }) {
  const [showHistory, setShowHistory] = useState(false);
  const history = useQuery({
    queryKey: ["doctor", "history", check.check_id],
    queryFn: () => get<HistoryResponse>(`/api/doctor/checks/${encodeURIComponent(check.check_id)}/history?limit=20`),
    enabled: showHistory,
  });
  const evidence = Object.entries(check.observed_evidence ?? {});
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          {check.title} <Mono>{check.check_id}</Mono>
        </span>
      }
      actions={
        <Button variant="ghost" onClick={onClose} aria-label="Tutup detail">
          <X className="h-4 w-4" aria-hidden />
        </Button>
      }
    >
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge status={check.effective_status} />
          {check.effective_status !== check.status && (
            <span className="text-xs text-ink-3">
              (tersimpan: <Badge status={check.status} />)
            </span>
          )}
          <SeverityBadge severity={check.severity} />
          {check.functional_only && <span className="text-xs text-ink-3">uji fungsi (opt-in)</span>}
        </div>
        <p className="text-sm">{check.summary}</p>
        {check.stale_reason && <Notice tone="warn">{check.stale_reason}</Notice>}
        <KeyValue
          items={[
            ["Grup", check.group_label],
            ["Komponen", check.component_id ? <Link className="text-accent hover:underline" to={`/tools/${check.component_id}`}>{check.component_id}</Link> : "—"],
            ["Versi komponen", check.component_version ?? "UNKNOWN"],
            ["Target", check.target_identity ?? "—"],
            ["Scope domain", typeof check.scope?.domain === "string" ? check.scope.domain : "platform (semua domain)"],
            ["Capability yang membaca hasil ini", Array.isArray(check.scope?.capabilities) && check.scope.capabilities.length ? (check.scope.capabilities as string[]).join(", ") : "—"],
            ["Diperiksa", <Freshness key="f" at={check.last_checked_at} staleAfterSeconds={3600} />],
            ["Berlaku sampai", check.expires_at ? `${absoluteTime(check.expires_at)} (${relativeTime(check.expires_at)})` : "—"],
            ["Invalidator", check.invalidators.length ? check.invalidators.join(", ") : "—"],
            ["Kode error", check.error_code ?? "—"],
            ["Kritis untuk", check.critical_for.length ? check.critical_for.join(", ") : "—"],
          ]}
        />
        {evidence.length > 0 && (
          <div>
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-3">Bukti teramati (teredaksi)</div>
            <pre className="mono max-h-56 overflow-auto rounded-md bg-surface-2 p-2 text-xs">{JSON.stringify(check.observed_evidence, null, 2)}</pre>
          </div>
        )}
        <div>
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-3">Usulan perbaikan</div>
          <RemedyPanel remedy={check.remediation} requiresDestructive={check.requires_destructive} requiresPaidWrite={check.requires_paid_write} />
        </div>
        <div>
          <button className="inline-flex items-center gap-1 text-xs text-accent hover:underline" onClick={() => setShowHistory(!showHistory)}>
            {showHistory ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
            Riwayat pemeriksaan
          </button>
          {showHistory && history.isLoading && <Loading />}
          {showHistory && history.data && (
            <ul className="mt-2 space-y-1 text-xs">
              {history.data.items.length === 0 && <li className="text-ink-3">Belum ada riwayat.</li>}
              {history.data.items.map((h) => (
                <li key={h.id} className="flex flex-wrap items-center gap-2">
                  <Badge status={h.status} />
                  <span title={absoluteTime(h.last_checked_at)}>{relativeTime(h.last_checked_at)}</span>
                  <span className="text-ink-3">{h.summary}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Card>
  );
}

function RunControls({ groups }: { groups: { id: string; label: string }[] }) {
  const qc = useQueryClient();
  const [group, setGroup] = useState("");
  const [functional, setFunctional] = useState(false);
  const run = useMutation({
    mutationFn: () => post("/api/doctor/run", { group: group || null, functional }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["doctor"] });
      qc.invalidateQueries({ queryKey: ["inventory"] });
    },
  });
  return (
    <div className="flex flex-wrap items-center gap-2">
      <select aria-label="Grup pemeriksaan" className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm" value={group} onChange={(e) => setGroup(e.target.value)}>
        <option value="">Semua grup</option>
        {groups.map((g) => (
          <option key={g.id} value={g.id}>
            {g.label}
          </option>
        ))}
      </select>
      <label className="flex items-center gap-1 text-sm" title="Uji fungsi memakai kuota Claude Max dan probe GitHub read-only lewat jaringan">
        <input type="checkbox" checked={functional} onChange={(e) => setFunctional(e.target.checked)} />
        Termasuk uji fungsi
      </label>
      <Button variant="primary" busy={run.isPending} onClick={() => run.mutate()}>
        <Play className="h-4 w-4" aria-hidden /> Jalankan pemeriksaan
      </Button>
      {functional && <span className="text-xs text-warn">Uji fungsi memakai kuota Max dan jaringan (read-only).</span>}
      {run.error && <ErrorBox error={run.error} />}
    </div>
  );
}

export default function ReadinessPage() {
  const { isAdmin, domain } = useAuth();
  const qc = useQueryClient();
  const checks = useQuery({ queryKey: ["doctor", "checks", domain], queryFn: () => get<ChecksData>("/api/doctor/checks") });
  const caps = useQuery({ queryKey: ["doctor", "capabilities", domain], queryFn: () => get<CapabilitiesResponse>("/api/doctor/capabilities") });
  const [selected, setSelected] = useState<string | null>(null);
  const [onlyProblems, setOnlyProblems] = useState(false);
  useEventStream((e) => {
    if (e.kind.startsWith("doctor.")) qc.invalidateQueries({ queryKey: ["doctor"] });
  });

  const grouped = useMemo(() => {
    const items = checks.data?.items ?? [];
    const filtered = onlyProblems ? items.filter((i) => !isOk(i)) : items;
    return (checks.data?.groups ?? [])
      .map((g) => ({ ...g, items: filtered.filter((i) => i.group === g.id) }))
      .filter((g) => g.items.length > 0);
  }, [checks.data, onlyProblems]);
  const selectedCheck = checks.data?.items.find((i) => i.check_id === selected) ?? null;

  if (checks.isLoading || caps.isLoading) return <Loading />;
  if (checks.error) return <ErrorBox error={checks.error} />;
  const lastRun = checks.data?.last_run;
  const lastFailure = checks.data?.last_failure;

  return (
    <div>
      <PageHeader
        title="Kesiapan (Readiness Doctor)"
        question="Apa yang belum siap dan bagaimana menyelesaikannya?"
        source={checks.data?.source}
        checkedAt={checks.data?.checked_at ?? null}
        evidenceLimit="CONNECTED ≠ VERIFIED; VERIFIED hanya untuk skenario yang disebut; UNKNOWN tidak dianggap lulus"
        actions={isAdmin ? <RunControls groups={checks.data?.groups ?? []} /> : <span className="text-xs text-ink-3">VIEWER: hanya baca</span>}
      />
      {!lastRun && (
        <Notice tone="warn">
          Doctor belum pernah dijalankan pada database ini; semua status UNKNOWN. Jalankan <Mono>aeos doctor</Mono> di terminal
          {isAdmin ? " atau tombol “Jalankan pemeriksaan”." : " (butuh SUPER_ADMIN)."}
        </Notice>
      )}
      {lastFailure && (
        <Notice tone="warn">
          <span data-testid="last-failure">
            Pemeriksaan {triggerLabel(lastFailure.trigger)} gagal pada {absoluteTime(lastFailure.at)}
            {lastFailure.error ? ` (${lastFailure.error})` : ""}; hasil di bawah berasal dari run sebelumnya dan bukan status
            terkini. Jalankan ulang pemeriksaan.
          </span>
        </Notice>
      )}
      {lastRun && (
        <p className="mb-3 text-xs text-ink-3" data-testid="last-run">
          Run terakhir: {absoluteTime(lastRun.finished_at)} via {triggerLabel(lastRun.trigger)}
          {lastRun.functional ? " (dengan uji fungsi)" : " (tanpa uji fungsi)"} —{" "}
          {Object.entries(lastRun.counts ?? {})
            .map(([k, v]) => `${k}: ${v}`)
            .join(", ")}
        </p>
      )}

      <Card title="Matriks kesiapan capability" className="mb-5">
        {caps.error ? (
          <ErrorBox error={caps.error} />
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-ink-3">
                <tr>
                  <th className="px-3 py-2">Capability</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Alasan</th>
                  <th className="px-3 py-2">Perbaikan</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {((caps.data?.items ?? []) as Capability[]).map((c) => (
                  <CapabilityRow key={c.capability} cap={c} />
                ))}
              </tbody>
            </table>
          </div>
        )}
        {caps.data?.human_only && (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-ink-3">
            <span>Bukan capability worker (keputusan manusia):</span>
            {caps.data.human_only.map((h) => (
              <span key={h.label} className="inline-flex items-center gap-1">
                <Badge status={h.status} /> {h.label}
              </span>
            ))}
          </div>
        )}
      </Card>

      <div className="mb-3 flex items-center gap-2">
        <label className="flex items-center gap-1 text-sm">
          <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} /> Tampilkan hanya yang bermasalah
        </label>
      </div>

      <div className={clsx("grid gap-5", selectedCheck && "lg:grid-cols-[minmax(0,1fr)_420px]")}>
        <div className="min-w-0 space-y-5">
          {grouped.length === 0 && <EmptyState title="Tidak ada pemeriksaan bermasalah pada filter ini." />}
          {grouped.map((g) => (
            <Card key={g.id} title={`${g.label} (${g.items.length})`}>
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead className="text-left text-xs uppercase tracking-wide text-ink-3">
                    <tr>
                      <th className="px-2 py-1.5">Pemeriksaan</th>
                      <th className="px-2 py-1.5">Status</th>
                      <th className="px-2 py-1.5">Severity</th>
                      <th className="px-2 py-1.5">Ringkasan</th>
                      <th className="px-2 py-1.5">Diperiksa / berlaku</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {g.items.map((c) => (
                      <tr
                        key={c.check_id}
                        className={clsx("cursor-pointer align-top hover:bg-surface-2", selected === c.check_id && "bg-surface-2")}
                        onClick={() => setSelected(c.check_id)}
                      >
                        <td className="px-2 py-2">
                          <div className="font-medium">{c.title}</div>
                          <div className="mono text-xs text-ink-3">{c.check_id}</div>
                          {c.component_id && (
                            <Link
                              to={`/tools/${c.component_id}`}
                              onClick={(e) => e.stopPropagation()}
                              className="inline-flex items-center gap-0.5 text-xs text-accent hover:underline"
                            >
                              {c.component_id} <ExternalLink className="h-3 w-3" aria-hidden />
                            </Link>
                          )}
                        </td>
                        <td className="px-2 py-2">
                          <Badge status={c.effective_status} />
                          {c.never_run && <div className="mt-0.5 text-xs text-ink-3">belum diperiksa</div>}
                          {!c.never_run && !isOk(c) && (
                            <div className="mt-0.5 text-xs text-warn" data-testid={`not-ok-${c.check_id}`}>
                              belum memenuhi syarat ({(c.ok_statuses ?? []).join("/") || "tidak pernah lulus otomatis"})
                            </div>
                          )}
                        </td>
                        <td className="px-2 py-2">
                          <SeverityBadge severity={c.severity} />
                        </td>
                        <td className="px-2 py-2">
                          <div>{c.summary}</div>
                          {c.stale_reason && <div className="text-xs text-warn">{c.stale_reason}</div>}
                        </td>
                        <td className="px-2 py-2 text-xs">
                          <Freshness at={c.last_checked_at} staleAfterSeconds={3600} />
                          {c.expires_at && <div className="text-ink-3">berlaku {relativeTime(c.expires_at)}</div>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          ))}
        </div>
        {selectedCheck && (
          <div className="lg:sticky lg:top-0 lg:self-start">
            <CheckDetail check={selectedCheck} onClose={() => setSelected(null)} />
          </div>
        )}
      </div>
      <ProjectResults items={checks.data?.project_items ?? []} />
      <ClaudeReview review={checks.data?.claude_review ?? null} />
      {checks.data?.status_legend && (
        <Card title="Arti status" className="mt-5">
          <KeyValue items={Object.entries(checks.data.status_legend).map(([k, v]) => [<Badge key={k} status={k} />, v])} />
        </Card>
      )}
    </div>
  );
}

/** §08 per-target readiness: one result per project integration target (repository, Drive root). */
export function ProjectResults({ items }: { items: ProjectCheckItem[] }) {
  if (!items.length) return null;
  return (
    <Card title={`Target per proyek (${items.length})`} className="mt-5">
      <ul className="space-y-1 text-sm" data-testid="project-results">
        {items.map((c) => (
          <li key={`${c.check_id}-${c.project_id}`} className="flex flex-wrap items-center gap-2">
            <Badge status={c.effective_status} />
            <span className="font-medium">{c.title}</span>
            <Mono>{c.project_id.slice(0, 8)}</Mono>
            <span className="text-xs text-ink-3">{c.summary}</span>
            <Freshness at={c.last_checked_at} staleAfterSeconds={3600} />
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** §07 optional Claude explanation (E0 advisory): never a status, never AUTH, never a remedy run. */
export function ClaudeReview({ review }: { review: ClaudeReviewData | null }) {
  if (!review) return null;
  return (
    <Card title="Penjelasan Claude (advisory, E0)" className="mt-5">
      <Notice tone="info">{review.note}</Notice>
      {review.ok ? (
        <>
          <p className="mt-2 text-sm">{review.summary}</p>
          <ul className="mt-2 list-disc pl-5 text-sm" data-testid="claude-review-findings">
            {review.findings.map((f, i) => (
              <li key={i}>
                <Mono>{f.check_id}</Mono> {f.observation} — <span className="text-ink-3">{f.suggestion}</span>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="mt-2 text-sm text-ink-3">Review tidak tersedia ({review.error_code ?? "bridge"}); gunakan paket manual.</p>
      )}
      <p className="mt-1 text-xs text-ink-3">{review.at ? relativeTime(review.at) : ""} · model {review.model_resolved ?? "UNKNOWN"}</p>
    </Card>
  );
}
