import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardList, FilePlus2, Sparkles } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, Freshness, inputBase, inputClass, Loading, Mono, Notice, Table } from "../../components/ui";
import { shortId } from "./labels";
import type { CheckpointCandidates, CheckpointOffer, CheckpointsResponse, CheckpointView, ClaudeAdvisory } from "./types";

const KIND_LABEL: Record<string, string> = { MILESTONE: "Milestone diterima", ARCHITECTURE_SNAPSHOT: "Snapshot arsitektur disetujui" };

/**
 * Checkpoint dokumentasi (master §28 "narasi/diagram terpadu pada checkpoint yang disetujui", §41 "delta pada
 * PR/checkpoint, finalisasi pada accepted milestone; Tech Writer/Diagram Author menyusun narasi; Claude memeriksa
 * paket perubahan material"). Recording is an explicit admin action on an ACCEPTED milestone / APPROVED snapshot;
 * Claude's review is advisory; the writer draft runs only after the owner approves the plan version that carries it.
 */
export function CheckpointPanel({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const { isAdmin } = useAuth();
  const list = useQuery({
    queryKey: ["doc-checkpoints", projectId],
    queryFn: () => get<CheckpointsResponse>(`/api/projects/${projectId}/documents/checkpoints`),
  });
  const candidates = useQuery({
    queryKey: ["doc-checkpoint-candidates", projectId],
    queryFn: () => get<CheckpointCandidates>(`/api/projects/${projectId}/documents/checkpoints/candidates`),
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["doc-checkpoints", projectId] });
    qc.invalidateQueries({ queryKey: ["doc-checkpoint-candidates", projectId] });
    qc.invalidateQueries({ queryKey: ["project-documents", projectId] });
  };
  const record = useMutation({
    mutationFn: (o: { plan_version_id: string; kind: string; ref: string }) =>
      post(`/api/projects/${projectId}/documents/checkpoints`, o),
    onSuccess: refresh,
  });
  if (list.isLoading || candidates.isLoading) return <Loading />;
  if (list.error) return <ErrorBox error={list.error} />;
  const items = list.data?.items ?? [];
  const cand = candidates.data;
  return (
    <div className="space-y-4">
      <Card
        title={<span className="flex items-center gap-2"><ClipboardList className="h-4 w-4" aria-hidden /> Checkpoint dokumentasi</span>}
        actions={<Freshness at={cand?.checked_at ?? null} label="Diperiksa" staleAfterSeconds={900} />}
      >
        <div className="space-y-3">
          <Notice tone="info">
            Checkpoint hanya dicatat untuk milestone yang seluruh task-nya ACCEPTED atau snapshot arsitektur yang disetujui manusia.
            Paket perubahan bersifat immutable; draft Tech Writer/Diagram Author baru dijalankan setelah pemilik menyetujuinya di
            versi rencana. Tinjauan Claude hanya saran, bukan keputusan.
          </Notice>
          <ErrorBox error={candidates.error} />
          <ErrorBox error={record.error} />
          {cand && !cand.plan_version && (
            <Notice tone="warn">Belum ada PlanVersion yang disetujui untuk proyek ini; checkpoint belum dapat dicatat.</Notice>
          )}
          {cand?.offers.map((o) => (
            <OfferNotice key={`${o.kind}:${o.ref}`} offer={o} isAdmin={isAdmin} busy={record.isPending}
              onRecord={() => record.mutate({ plan_version_id: o.plan_version_id, kind: o.kind, ref: o.ref })} />
          ))}
          {cand && cand.milestones.length > 0 && (
            <Table
              rows={cand.milestones}
              rowKey={(m) => m.key}
              empty="Rencana yang disetujui tidak memuat milestone."
              columns={[
                { header: "Milestone", cell: (m) => <span><Mono>{m.key}</Mono> {m.title}</span> },
                {
                  header: "Task diterima",
                  cell: (m) => (
                    <StatusBadge status={m.accepted ? "ACCEPTED" : "PENDING"}
                      label={`${m.tasks_accepted}/${m.tasks_total} ACCEPTED${m.tasks_total === 0 ? " (belum ada TaskSpec)" : ""}`} />
                  ),
                },
                {
                  header: "Checkpoint",
                  cell: (m) => (m.checkpoint_id
                    ? <StatusBadge status="CURRENT" label={`Tercatat ${shortId(m.checkpoint_id)}`} />
                    : <StatusBadge status="NOT_RECORDED" label="Belum tercatat" />),
                },
              ]}
            />
          )}
          {cand && cand.snapshots.length > 0 && (
            <Table
              rows={cand.snapshots}
              rowKey={(s) => s.id}
              columns={[
                { header: "Snapshot arsitektur", cell: (s) => <span>{s.label || "—"} <Mono>{shortId(s.id)}</Mono></span> },
                { header: "Approval manusia", cell: (s) => <StatusBadge status={s.approved ? "APPROVED" : "NOT_APPROVED"} label={s.approved ? "Disetujui (hash terikat)" : "Belum disetujui"} /> },
                {
                  header: "Checkpoint",
                  cell: (s) => (s.checkpoint_id
                    ? <StatusBadge status="CURRENT" label={`Tercatat ${shortId(s.checkpoint_id)}`} />
                    : <StatusBadge status="NOT_RECORDED" label="Belum tercatat" />),
                },
              ]}
            />
          )}
          {isAdmin && cand?.plan_version && (
            <RecordForm candidates={cand} busy={record.isPending}
              onRecord={(kind, ref) => record.mutate({ plan_version_id: cand.plan_version!.id, kind, ref })} />
          )}
          <p className="text-xs text-ink-3">Sumber: {cand?.source ?? "—"} · CLI: <Mono>aeos docs checkpoint &lt;plan_version_id&gt; --kind MILESTONE --ref &lt;key&gt;</Mono></p>
        </div>
      </Card>
      {items.length === 0 ? (
        <EmptyState title="Belum ada checkpoint dokumentasi">Paket perubahan muncul di sini setelah checkpoint pertama dicatat.</EmptyState>
      ) : (
        items.map((cp) => <PackageCard key={cp.id} cp={cp} projectId={projectId} isAdmin={isAdmin} onChanged={refresh} />)
      )}
      <p className="text-xs text-ink-3">Sumber: {list.data?.source}</p>
    </div>
  );
}

function OfferNotice({ offer, isAdmin, busy, onRecord }: { offer: CheckpointOffer; isAdmin: boolean; busy: boolean; onRecord: () => void }) {
  return (
    <Notice tone="warn">
      <div className="flex flex-wrap items-center gap-2">
        <span>{offer.reason}</span>
        {isAdmin ? (
          <Button className="text-xs" busy={busy} onClick={onRecord}>Catat checkpoint {offer.ref}</Button>
        ) : (
          <span className="text-xs">Pencatatan dilakukan oleh SUPER_ADMIN.</span>
        )}
      </div>
    </Notice>
  );
}

function RecordForm({ candidates, busy, onRecord }: { candidates: CheckpointCandidates; busy: boolean; onRecord: (kind: string, ref: string) => void }) {
  const [kind, setKind] = useState<string>(candidates.milestones.length ? "MILESTONE" : "ARCHITECTURE_SNAPSHOT");
  const options = kind === "MILESTONE"
    ? candidates.milestones.map((m) => ({ value: m.key, label: `${m.key} — ${m.tasks_accepted}/${m.tasks_total} ACCEPTED` }))
    : candidates.snapshots.map((s) => ({ value: s.id, label: `${s.label || shortId(s.id)}${s.approved ? "" : " (belum disetujui)"}` }));
  const [ref, setRef] = useState("");
  const chosen = options.some((o) => o.value === ref) ? ref : options[0]?.value ?? "";
  return (
    <div className="flex flex-wrap items-end gap-2">
      <Field label="Jenis checkpoint">
        <select className={`${inputBase} w-full sm:w-56`} value={kind} onChange={(e) => { setKind(e.target.value); setRef(""); }}>
          <option value="MILESTONE">{KIND_LABEL.MILESTONE}</option>
          {candidates.snapshots.length > 0 && <option value="ARCHITECTURE_SNAPSHOT">{KIND_LABEL.ARCHITECTURE_SNAPSHOT}</option>}
        </select>
      </Field>
      <Field label="Referensi" hint="Ditolak bila belum diterima/disetujui (fail closed).">
        <select aria-label="Referensi checkpoint" className={`${inputBase} w-full sm:w-72`} value={chosen} onChange={(e) => setRef(e.target.value)}>
          {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </Field>
      <Button variant="primary" busy={busy} disabled={!chosen} onClick={() => onRecord(kind, chosen)}>Catat checkpoint</Button>
    </div>
  );
}

function PackageCard({ cp, projectId, isAdmin, onChanged }: { cp: CheckpointView; projectId: string; isAdmin: boolean; onChanged: () => void }) {
  const [rationale, setRationale] = useState("");
  const review = useMutation({
    mutationFn: () => post<ClaudeAdvisory>("/api/claude/review-docs-checkpoint", { revision_id: cp.id }),
  });
  const plan = useMutation({
    mutationFn: () => post<{ plan_version_id: string; version: number; note: string }>(
      `/api/projects/${projectId}/documents/checkpoints/${cp.id}/writer-task`, { rationale }),
    onSuccess: onChanged,
  });
  const task = cp.writer_task;
  const planned = cp.writer_task_planned;
  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-2">
          {KIND_LABEL[cp.kind] ?? cp.kind} <Mono>{cp.ref}</Mono>
          <StatusBadge status={cp.material ? "REVIEW" : "SKIPPED"} label={cp.material ? "Perubahan material" : "Tidak material"} />
          {cp.finalised && <StatusBadge status="CURRENT" label="Dokumentasi milestone difinalisasi" />}
        </span>
      }
      actions={<Freshness at={cp.created_at} label="Dicatat" staleAfterSeconds={86400 * 365} />}
    >
      <div className="space-y-3 text-sm">
        <p className="text-xs text-ink-3">Paket <Mono>{cp.package_hash}</Mono> · PlanVersion <Mono>{shortId(cp.plan_version_id)}</Mono> (immutable)</p>
        <div>
          <h4 className="font-medium">Dokumen berubah ({cp.changed_documents.length})</h4>
          {cp.changed_documents.length === 0 ? (
            <p className="text-xs text-ink-3">Tidak ada dokumen yang berubah sejak checkpoint sebelumnya.</p>
          ) : (
            <ul className="list-disc pl-5">
              {cp.changed_documents.slice(0, 30).map((c) => (
                <li key={c.logical_doc_id}><Mono>{c.logical_doc_id}</Mono> ({c.change === "ADDED" ? "baru" : "berubah"}): {c.sections.join(", ") || "—"}</li>
              ))}
            </ul>
          )}
        </div>
        <p>Diagram berubah: {cp.diagram_delta.length ? cp.diagram_delta.map((d) => <Mono key={d}>{d}</Mono>) : "—"}</p>
        {cp.contract_deltas.length > 0 && <p>Delta kontrak: {cp.contract_deltas.map((d) => <Mono key={d}>{d}</Mono>)}</p>}
        {cp.architecture_delta?.nodes && (
          <p>Delta arsitektur: +{cp.architecture_delta.nodes.added.length} / −{cp.architecture_delta.nodes.removed.length} / ~{cp.architecture_delta.nodes.changed.length} node</p>
        )}
        {task && (
          <div className="rounded-md border border-line p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">Draft Tech Writer / Diagram Author</span>
              <StatusBadge status="DRAFT" label="Draft — dijalankan hanya setelah pemilik menyetujui versi rencana" />
            </div>
            <p className="mt-1">{task.title}</p>
            <p className="text-xs text-ink-3">{task.goal} · role <Mono>{task.role_key}</Mono></p>
            {planned ? (
              <Notice tone="info">
                Sudah ditambahkan sebagai task <Mono>{planned.task_key}</Mono> di versi rencana v{planned.version} — menunggu keputusan pemilik
                di <Link to="/plans" className="underline">Rencana</Link>.
              </Notice>
            ) : (
              isAdmin && cp.material && (
                <div className="mt-2 flex flex-wrap items-end gap-2">
                  <Field label="Alasan (dicatat di versi rencana)">
                    <input className={inputClass} value={rationale} onChange={(e) => setRationale(e.target.value)} placeholder="Narasi checkpoint perlu diperbarui" />
                  </Field>
                  <Button busy={plan.isPending} onClick={() => plan.mutate()}>
                    <FilePlus2 className="h-4 w-4" aria-hidden /> Tambahkan ke versi rencana berikutnya
                  </Button>
                </div>
              )
            )}
            {plan.data && <Notice tone="info">{plan.data.note}</Notice>}
            <ErrorBox error={plan.error} />
          </div>
        )}
        {isAdmin && (
          <div className="space-y-2">
            <Button
              disabled={!cp.material}
              title={cp.material ? "Tinjauan Claude (saran) atas paket perubahan material" : "Hanya paket perubahan material yang ditinjau"}
              busy={review.isPending}
              onClick={() => review.mutate()}
            >
              <Sparkles className="h-4 w-4" aria-hidden /> Minta review Claude
            </Button>
            <ErrorBox error={review.error} />
            {review.data && <AdvisoryBox advice={review.data} />}
          </div>
        )}
      </div>
    </Card>
  );
}

function AdvisoryBox({ advice }: { advice: ClaudeAdvisory }) {
  if (advice.status !== "NARRATION") {
    return <Notice tone="warn">{advice.label} {advice.error_code ? <Mono>{advice.error_code}</Mono> : null}</Notice>;
  }
  return (
    <Notice tone="mock">
      <div className="space-y-1">
        <p className="font-medium">Narasi Claude — {advice.label}</p>
        <p>{advice.assessment}</p>
        {advice.concerns && advice.concerns.length > 0 && (
          <ul className="list-disc pl-5">{advice.concerns.map((c) => <li key={c}>{c}</li>)}</ul>
        )}
        {advice.questions_for_owner && advice.questions_for_owner.length > 0 && (
          <p className="text-xs">Pertanyaan untuk pemilik: {advice.questions_for_owner.join(" · ")}</p>
        )}
      </div>
    </Notice>
  );
}
