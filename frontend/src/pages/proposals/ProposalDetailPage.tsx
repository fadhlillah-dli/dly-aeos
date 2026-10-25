import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, RefreshCw } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { IDENTITY_STATUS, MappedBadge, PROPOSAL_STATUS, RESEARCH_LIFECYCLE } from "../../components/proposals/MappedBadge";
import { decisionHref, RelatedLinks } from "../../components/proposals/PageLinks";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, inputBase, inputClass, KeyValue, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { absoluteTime, shortHash } from "../../lib/format";
import { DecisionPanel } from "./DecisionPanel";
import { CheckpointCard, LinksCard, OwnerPlanCard, PossibleDuplicates, RemovalClosure, TrialCard } from "./LifecyclePanels";
import {
  AlternativesSection,
  BaselineSection,
  ClaudeSection,
  ContributorsNote,
  CostSection,
  ExperimentSection,
  ImpactSection,
  OutcomeSummary,
  SecuritySection,
  SourceSection,
  SummarySection,
} from "./ReviewSections";
import { hasPackage, type ProposalDetail, type Timeline } from "./types";

function ReviewControls({ p }: { p: ProposalDetail }) {
  const qc = useQueryClient();
  const [pin, setPin] = useState(p.candidate_pin ?? "");
  const [digest, setDigest] = useState(p.candidate_digest ?? "");
  const [withClaude, setWithClaude] = useState(false);
  const m = useMutation({
    mutationFn: () =>
      post<ProposalDetail>(`/api/proposals/${p.id}/review`, { with_claude: withClaude, candidate_pin: pin || null, candidate_digest: digest.trim() }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposal", p.id] }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    m.mutate();
  };
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
      <Field label="Pin/digest kandidat" hint="Perubahan pin membatalkan keputusan yang terikat">
        <input className={inputBase + " w-full sm:w-56"} value={pin} onChange={(e) => setPin(e.target.value)} placeholder="UNKNOWN" />
      </Field>
      <Field label="Digest kandidat (sha256)" hint="Wajib bila komponen dipin digest di install-manifest; dibandingkan dengan binary teramati saat ACCEPTED">
        <input
          className={inputBase + " mono w-full sm:w-80"}
          value={digest}
          onChange={(e) => setDigest(e.target.value)}
          placeholder="sha256:… (belum tercatat)"
          aria-label="Digest kandidat"
          pattern="^(sha256:[0-9a-fA-F]{64})?$"
        />
      </Field>
      <label className="flex items-center gap-2 pb-2 text-sm">
        <input type="checkbox" checked={withClaude} onChange={(e) => setWithClaude(e.target.checked)} disabled={p.domain === "OFFICE"} />
        Minta rekomendasi Claude (metadata publik saja{p.domain === "OFFICE" ? "; tidak tersedia untuk OFFICE" : ""})
      </label>
      <Button type="submit" variant="primary" busy={m.isPending}>
        <RefreshCw className="h-4 w-4" aria-hidden /> {hasPackage(p) ? "Review ulang" : "Buat paket review"}
      </Button>
      <ErrorBox error={m.error} />
    </form>
  );
}

function EvidenceForm({ id }: { id: string }) {
  const qc = useQueryClient();
  const [ids, setIds] = useState("");
  const m = useMutation({
    mutationFn: () => post(`/api/proposals/${id}/evidence`, { evidence_ids: ids.split(/[\s,]+/).filter(Boolean) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposal", id] }),
  });
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        m.mutate();
      }}
    >
      <Field label="ID bukti terpercaya (verifier/collector)" hint="Bukti worker/MOCK ditolak">
        <input className={inputBase + " w-full sm:w-80"} value={ids} onChange={(e) => setIds(e.target.value)} required />
      </Field>
      <Button type="submit" busy={m.isPending}>
        Lampirkan bukti trial
      </Button>
      <ErrorBox error={m.error} />
    </form>
  );
}

function OutcomeForm({ id, criteria }: { id: string; criteria: { id: string; criterion: string }[] }) {
  const qc = useQueryClient();
  const [status, setStatus] = useState("ACCEPTED");
  const [evidence, setEvidence] = useState("");
  const [config, setConfig] = useState("");
  const [failures, setFailures] = useState("");
  const [dataRollback, setDataRollback] = useState("UNKNOWN");
  const [codeReverted, setCodeReverted] = useState(true);
  const [notes, setNotes] = useState("");
  const [results, setResults] = useState<Record<string, { status: string; evidence_id: string }>>({});
  const [waiver, setWaiver] = useState("");
  const [adrs, setAdrs] = useState("");
  const [rollbackRef, setRollbackRef] = useState("");
  const [restoreEvidence, setRestoreEvidence] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const m = useMutation({
    mutationFn: (body: Record<string, unknown>) => post(`/api/proposals/${id}/outcome`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposal", id] }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setParseError(null);
    let accepted: Record<string, unknown> | null = null;
    if (status === "ACCEPTED") {
      try {
        accepted = config ? (JSON.parse(config) as Record<string, unknown>) : null;
      } catch {
        setParseError("Konfigurasi diterima harus JSON yang valid");
        return;
      }
    }
    m.mutate({
      status,
      evidence_ids: evidence.split(/[\s,]+/).filter(Boolean),
      accepted_configuration: accepted,
      partial_failures: status === "PARTIAL" ? failures.split("\n").map((x) => x.trim()).filter(Boolean) : [],
      rollback:
        status === "ROLLED_BACK"
          ? {
              code_reverted: codeReverted,
              data_rollback: dataRollback,
              reference: { audit_event_id: rollbackRef },
              ...(restoreEvidence ? { restore_evidence_id: restoreEvidence } : {}),
            }
          : null,
      acceptance_results: status === "ACCEPTED" ? results : {},
      waiver_decision_id: waiver || null,
      adr_keys: adrs.split(/[\s,]+/).filter(Boolean),
      notes,
    });
  };
  return (
    <form onSubmit={submit} className="space-y-3" aria-label="Catat hasil">
      <Notice tone="warn">Rollback tidak pernah dicatat sebagai penerimaan; rollback kode tidak menjamin rollback data.</Notice>
      <div className="grid gap-3 md:grid-cols-3">
        <Field label="Hasil">
          <select className={inputClass} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="ACCEPTED">ACCEPTED — diterima</option>
            <option value="ROLLED_BACK">ROLLED_BACK — di-rollback</option>
            <option value="FAILED">FAILED — gagal</option>
            <option value="PARTIAL">PARTIAL — sebagian</option>
          </select>
        </Field>
        <Field label="ID bukti terpercaya">
          <input className={inputClass} value={evidence} onChange={(e) => setEvidence(e.target.value)} />
        </Field>
        {status === "ACCEPTED" && (
          <Field label="Konfigurasi diterima (JSON)">
            <input className={inputClass} value={config} onChange={(e) => setConfig(e.target.value)} placeholder='{"component_id":"…","approved_pin":"…"}' />
          </Field>
        )}
        {status === "ROLLED_BACK" && (
          <>
            <Field label="Rollback data">
              <select className={inputClass} value={dataRollback} onChange={(e) => setDataRollback(e.target.value)}>
                <option value="UNKNOWN">UNKNOWN</option>
                <option value="DONE">DONE</option>
                <option value="NOT_POSSIBLE">NOT_POSSIBLE</option>
                <option value="NOT_APPLICABLE">NOT_APPLICABLE</option>
              </select>
            </Field>
            <label className="flex items-center gap-2 pt-6 text-sm">
              <input type="checkbox" checked={codeReverted} onChange={(e) => setCodeReverted(e.target.checked)} /> Kode di-revert
            </label>
            <Field label="ID audit operasi rollback/restore yang dijalankan" hint="ops.release.rollback / ops.backup.restore / inventory.manifest.load">
              <input className={inputClass} value={rollbackRef} onChange={(e) => setRollbackRef(e.target.value)} required />
            </Field>
            {dataRollback === "DONE" && (
              <Field label="ID bukti restore teruji">
                <input className={inputClass} value={restoreEvidence} onChange={(e) => setRestoreEvidence(e.target.value)} required />
              </Field>
            )}
          </>
        )}
        {status === "ACCEPTED" && (
          <>
            {criteria.map((c) => (
              <Field key={c.id} label={`${c.id}: ${c.criterion}`}>
                <div className="flex gap-1">
                  <select
                    aria-label={`Hasil ${c.id}`}
                    className={inputClass}
                    value={results[c.id]?.status ?? "UNKNOWN"}
                    onChange={(e) => setResults({ ...results, [c.id]: { status: e.target.value, evidence_id: results[c.id]?.evidence_id ?? "" } })}
                  >
                    <option value="UNKNOWN">UNKNOWN</option>
                    <option value="PASS">PASS</option>
                    <option value="FAIL">FAIL</option>
                  </select>
                  <input
                    aria-label={`Bukti ${c.id}`}
                    className={inputClass}
                    placeholder="ID bukti"
                    value={results[c.id]?.evidence_id ?? ""}
                    onChange={(e) => setResults({ ...results, [c.id]: { status: results[c.id]?.status ?? "UNKNOWN", evidence_id: e.target.value } })}
                  />
                </div>
              </Field>
            ))}
            <Field label="ADR terkait (ADD/REPLACE/REMOVE)">
              <input className={inputClass} value={adrs} onChange={(e) => setAdrs(e.target.value)} placeholder="ADR-00xx" />
            </Field>
            <Field label="Waiver (keputusan RISK_ACCEPTANCE) bila acceptance tidak terpenuhi">
              <input className={inputClass} value={waiver} onChange={(e) => setWaiver(e.target.value)} />
            </Field>
          </>
        )}
        {status === "PARTIAL" && (
          <Field label="Bagian/target yang gagal (satu per baris)">
            <textarea className={inputClass} rows={2} value={failures} onChange={(e) => setFailures(e.target.value)} required />
          </Field>
        )}
      </div>
      <Field label="Catatan">
        <textarea className={inputClass} rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
      </Field>
      {parseError && <ErrorBox error={new Error(parseError)} />}
      <ErrorBox error={m.error} />
      <Button type="submit" variant="primary" busy={m.isPending}>
        Catat hasil
      </Button>
    </form>
  );
}

function TimelineCard({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["proposal", id, "timeline"], queryFn: () => get<Timeline>(`/api/proposals/${id}/timeline`) });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const t = q.data;
  if (!t) return null;
  const ba = t.before_after;
  return (
    <div className="space-y-4">
      <div className="grid items-stretch gap-3 md:grid-cols-[1fr_auto_1fr]">
        <div className="rounded-md border border-line p-3">
          <div className="text-xs font-semibold uppercase text-ink-3">Sebelum (baseline)</div>
          <div className="text-xs">
            hash <Mono>{shortHash(ba.before.baseline_hash, 12)}</Mono>
          </div>
          <ul className="mt-1 list-disc pl-5 text-sm">
            {ba.before.components.map((c) => (
              <li key={c.component_id}>
                {c.name ?? c.component_id} — pin {c.approved_pin ?? "UNKNOWN"}
              </li>
            ))}
            {ba.before.components.length === 0 && <li>Tidak ada komponen lama</li>}
          </ul>
          {ba.snapshots.before && <div className="mt-1 text-xs">Snapshot: {ba.snapshots.before.label} ({shortHash(ba.snapshots.before.snapshot_hash)})</div>}
        </div>
        <div className="flex items-center justify-center">
          <ArrowRight className="h-5 w-5 text-ink-3" aria-label="menjadi" />
        </div>
        <div className="rounded-md border border-line p-3">
          <div className="text-xs font-semibold uppercase text-ink-3">Sesudah</div>
          {ba.after ? (
            <>
              <div className="text-xs">
                candidate <Mono>{shortHash(ba.after.candidate_hash, 12)}</Mono> pin {ba.after.candidate_pin ?? "UNKNOWN"} · digest{" "}
                {ba.after.candidate_digest ? <Mono>{shortHash(ba.after.candidate_digest, 12)}</Mono> : "belum tercatat"}
              </div>
              <Mono>{JSON.stringify(ba.after.accepted_configuration)}</Mono>
            </>
          ) : (
            <p className="text-sm text-ink-2">{ba.note}</p>
          )}
          <div className="mt-1">
            <MappedBadge value={ba.status} map={PROPOSAL_STATUS} />
          </div>
          {ba.snapshots.after && <div className="mt-1 text-xs">Snapshot: {ba.snapshots.after.label} ({shortHash(ba.snapshots.after.snapshot_hash)})</div>}
        </div>
      </div>
      <Table
        rows={t.items}
        rowKey={(i) => `${i.type}:${i.at}:${i.action}:${i.actor}`}
        empty="Belum ada riwayat"
        columns={[
          { header: "Waktu", cell: (i) => absoluteTime(i.at) },
          { header: "Jenis", cell: (i) => (i.type === "decision" ? "Keputusan" : "Audit") },
          { header: "Tindakan", cell: (i) => <Mono>{i.action ?? "—"}</Mono> },
          { header: "Oleh", cell: (i) => i.actor },
          { header: "Hasil", cell: (i) => <StatusBadge status={i.result === "SUCCESS" || i.result === "ACTIVE" ? "CONFIGURED" : i.result === "INVALIDATED" ? "STALE" : i.result} label={i.result} /> },
        ]}
      />
    </div>
  );
}

export default function ProposalDetailPage() {
  const { proposalId = "" } = useParams();
  const { isAdmin } = useAuth();
  const q = useQuery({ queryKey: ["proposal", proposalId], queryFn: () => get<ProposalDetail>(`/api/proposals/${proposalId}`) });
  const p = q.data;
  if (q.isLoading) return <Loading />;
  if (q.error || !p)
    return (
      <div>
        <PageHeader title="Detail usulan" question="Paket review, dampak, eksperimen, keputusan dan hasil" />
        <ErrorBox error={q.error ?? new Error("Usulan tidak ditemukan")} />
      </div>
    );
  const pkg = hasPackage(p) ? p.review_package : null;
  return (
    <div className="space-y-5">
      <PageHeader
        title={p.title}
        question="Paket review, dampak, eksperimen, keputusan dan hasil"
        source="change_proposals + inventory + research registry + decisions"
        checkedAt={p.reviewed_at}
        evidenceLimit={p.guarantees.note}
        relatedDecisionsHref={`/decisions?target_type=change_proposal&target_id=${encodeURIComponent(p.id)}`}
        actions={
          <>
            <Link to="/proposals" className="self-center">
              ← Semua usulan
            </Link>
            <RelatedLinks subject={`Koreksi usulan: ${p.title}`} decisionId={p.decisions.length ? p.decisions[p.decisions.length - 1].id : null} />
          </>
        }
      />
      <Card title="Ringkasan usulan">
        <KeyValue
          items={[
            ["Status", <MappedBadge key="s" value={p.status} map={PROPOSAL_STATUS} />],
            ["Jenis", <Mono key="k">{p.kind}</Mono>],
            ["Domain / cakupan", `${p.domain} · ${p.scope_type === "PROJECT" ? `proyek ${p.project_id}` : "platform"}`],
            ["Sumber", p.source_refs.length ? p.source_refs.join(", ") : "—"],
            ["Identitas kanonik", p.canonical_identities.map((i) => <div key={i}><Mono>{i}</Mono></div>)],
            ["Komponen lama", p.existing_component_ids.length ? p.existing_component_ids.map((c) => <Link key={c} to={`/tools/${c}`} className="mr-2">{c}</Link>) : "—"],
            ["Komponen kandidat (ditambah/pengganti)", (p.candidate_component_ids ?? []).length ? (p.candidate_component_ids ?? []).map((c) => <Link key={c} to={`/tools/${c}`} className="mr-2">{c}</Link>) : "—"],
            ["Kapan aktif", p.delivery_dimensions?.ACTIVE?.at ? absoluteTime(p.delivery_dimensions.ACTIVE.at) : "belum aktif (tidak ada observasi ENABLED dari inventaris)"],
            ["Duplikat dari", p.duplicate_of ? <Link to={`/proposals/${p.duplicate_of}`}>{p.duplicate_of.slice(0, 8)}</Link> : "—"],
            ["Usulan duplikat yang tertaut", p.duplicates.length ? p.duplicates.map((d) => <Link key={d.id} to={`/proposals/${d.id}`} className="mr-2">{d.title}</Link>) : "—"],
            ["Field UNKNOWN", p.unknown_fields.length ? p.unknown_fields.join(", ") : "—"],
            ["Diajukan oleh", p.submitted_by],
          ]}
        />
        {p.parts.length > 0 && (
          <div className="mt-3">
            <div className="mb-1 text-sm font-medium">Bagian perubahan (satu approval di parent)</div>
            <ul className="list-disc pl-5 text-sm">
              {p.parts.map((pt) => (
                <li key={pt.child_id}>
                  <Link to={`/proposals/${pt.child_id}`}>{pt.title}</Link> ({pt.kind}){pt.depends_on.length ? ` — bergantung pada ${pt.depends_on.map((d) => d.slice(0, 8)).join(", ")}` : ""}
                </li>
              ))}
            </ul>
          </div>
        )}
        {p.research_entries.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2 text-sm">
            {p.research_entries.map((r) => (
              <span key={r.id} className="inline-flex items-center gap-1">
                {r.name} <MappedBadge value={r.lifecycle} map={RESEARCH_LIFECYCLE} /> <MappedBadge value={r.identity_status} map={IDENTITY_STATUS} />
              </span>
            ))}
          </div>
        )}
        <PossibleDuplicates p={p} />
        {isAdmin && !p.parent_id && !["ACCEPTED", "ROLLED_BACK", "FAILED", "PARTIAL", "PROMOTION_APPROVED"].includes(p.status) && (
          <div className="mt-4 border-t border-line pt-3">
            <ReviewControls p={p} />
          </div>
        )}
        {p.parent_id && <Notice>Ini bagian dari usulan induk; review dan keputusan dilakukan pada <Link to={`/proposals/${p.parent_id}`}>usulan induk</Link>.</Notice>}
      </Card>

      {!pkg ? (
        <EmptyState title="Paket review belum dibuat">
          {isAdmin ? "Tekan “Buat paket review” — dihitung deterministik dari inventaris dan registry riset." : "Menunggu SUPER_ADMIN membuat paket review."}
        </EmptyState>
      ) : (
        <>
          {pkg.binding_note && <Notice tone="warn">{pkg.binding_note}</Notice>}
          <SummarySection pkg={pkg} />
          <SourceSection pkg={pkg} />
          <BaselineSection pkg={pkg} />
          <AlternativesSection pkg={pkg} />
          <ImpactSection pkg={pkg} />
          <SecuritySection pkg={pkg} />
          <ExperimentSection pkg={pkg} />
          {pkg.removal_plan && pkg.removal_plan.items.length > 0 && (
            <Card title="Penutupan item REMOVE (progres, tidak mengubah candidate hash)">
              <RemovalClosure p={p} items={pkg.removal_plan.items} isAdmin={isAdmin} />
            </Card>
          )}
          <OwnerPlanCard p={p} isAdmin={isAdmin} />
          <CostSection pkg={pkg} />
          <ClaudeSection pkg={pkg} />
          <ContributorsNote pkg={pkg} />
          <Card title={<span><span className="mr-2 inline-flex h-5 w-5 items-center justify-center rounded-full bg-surface-3 text-xs">9</span>Keputusan manusia yang diperlukan</span>}>
            <DecisionPanel proposalId={p.id} pkg={pkg} allowed={p.allowed_actions} isAdmin={isAdmin} />
            {isAdmin && ["TRIAL_APPROVED", "IN_TRIAL", "PROMOTION_PENDING"].includes(p.status) && (
              <div className="mt-4 border-t border-line pt-3">
                <EvidenceForm id={p.id} />
              </div>
            )}
          </Card>
          <TrialCard p={p} isAdmin={isAdmin} />
          <CheckpointCard p={p} isAdmin={isAdmin} />
          <LinksCard p={p} isAdmin={isAdmin} />
          <Card title={<span><span className="mr-2 inline-flex h-5 w-5 items-center justify-center rounded-full bg-surface-3 text-xs">10</span>Hasil setelah implementasi</span>}>
            <OutcomeSummary pkg={{ ...pkg, outcome: p.outcome.current ?? pkg.outcome }} />
            <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {Object.entries(p.delivery_dimensions).map(([k, d]) => (
                <div key={k} className="rounded-md border border-line p-2 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <Mono>{k}</Mono>
                    <StatusBadge
                      status={
                        ["YES", "ACCEPTED", "EVIDENCE_ATTACHED", "ENABLED_IN_CONFIG", "OBSERVED_IN_CONFIG", "PUBLISHED"].includes(d.status)
                          ? "CONFIGURED"
                          : d.status === "NOT-PROVEN"
                            ? "NOT-PROVEN"
                            : ["PARTIAL", "CONFLICT"].includes(d.status)
                              ? d.status
                              : d.status === "NO"
                                ? "PENDING"
                                : "UNKNOWN"
                      }
                      label={d.status}
                    />
                  </div>
                  <div className="text-xs text-ink-3">{d.note}</div>
                </div>
              ))}
            </div>
            {isAdmin && p.kind !== "COMPARE" && ["PROMOTION_APPROVED", "ACCEPTED", "PARTIAL", "TRIAL_APPROVED", "PROMOTION_PENDING"].includes(p.status) && (
              <div className="mt-4 border-t border-line pt-3">
                <OutcomeForm id={p.id} criteria={p.owner_plan?.acceptance ?? []} />
              </div>
            )}
          </Card>
        </>
      )}

      <Card title="Keputusan tercatat">
        <Table
          rows={p.decisions}
          rowKey={(d) => d.id}
          empty="Belum ada keputusan"
          columns={[
            { header: "Waktu", cell: (d) => absoluteTime(d.created_at) },
            {
              header: "Jenis",
              cell: (d) => (
                <Link to={decisionHref(d.id)}>
                  <Mono>{d.kind}</Mono> {d.action}
                </Link>
              ),
            },
            { header: "Status", cell: (d) => <StatusBadge status={d.status === "ACTIVE" ? "CONFIGURED" : d.status === "INVALIDATED" ? "STALE" : d.status} label={d.status} /> },
            { header: "Terikat", cell: (d) => <span className="text-xs">baseline <Mono>{shortHash(d.baseline_hash)}</Mono> · candidate <Mono>{shortHash(d.candidate_hash)}</Mono></span> },
            { header: "Oleh / alasan", cell: (d) => <span className="text-xs">{d.actor_user_id} — {d.rationale}{d.invalidation_reason ? ` (dibatalkan: ${d.invalidation_reason})` : ""}</span> },
          ]}
        />
      </Card>

      <Card title="Riwayat & sebelum → sesudah">
        <TimelineCard id={p.id} />
      </Card>
    </div>
  );
}
