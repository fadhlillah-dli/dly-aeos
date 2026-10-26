import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { get, post, put } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { OpsBadge } from "../../components/control_center/OpsBadge";
import { Provenance } from "../../components/control_center/Provenance";
import { Button, EmptyState, ErrorBox, inputClass, Loading, Mono, Notice } from "../../components/ui";
import { absoluteTime, relativeTime } from "../../lib/format";
import type { RetirementChecklist, RetirementItem, RetirementStep } from "./types";

function detailText(detail: Record<string, unknown>): string {
  return Object.entries(detail)
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? (v.length ? v.join(", ") : "—") : v === null || v === undefined ? "—" : String(v)}`)
    .join(" · ");
}

/** Checklist items whose workflow step the owner records (PUT /api/ops/retirement/{id}/steps/{key}); the final
 * verification is the RETIREMENT decision, not a step. */
const STEP_KEYS = ["consumers", "jobs", "credentials", "repositories", "integrations", "infra", "data", "legal_hold", "backups", "communication"];
const STEP_STATUSES: [string, string, string][] = [
  ["DONE", "DONE", "Selesai"],
  ["IN_PROGRESS", "PENDING", "Sedang dikerjakan"],
  ["NOT_APPLICABLE", "NOT_APPLICABLE", "Tidak berlaku"],
];

function stepBadge(status: string) {
  const s = STEP_STATUSES.find(([v]) => v === status);
  return <OpsBadge status={s ? s[1] : status} label={s ? s[2] : undefined} />;
}

/** What the owner recorded for this step (status, owner, note, evidence) - a workflow record, not proof. */
export function RecordedStep({ step }: { step: RetirementStep }) {
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-ink-2" aria-label="Langkah tercatat">
      <span className="text-ink-3">Langkah tercatat:</span>
      {stepBadge(step.status)}
      <span>oleh {step.owner || step.by}</span>
      <span className="text-ink-3" title={absoluteTime(step.at)}>{relativeTime(step.at)}</span>
      {step.note && <span>· {step.note}</span>}
      {step.evidence_ids.length > 0 && (
        <span>
          · bukti:{" "}
          {step.evidence_ids.map((id) => (
            <Mono key={id}>{id}</Mono>
          ))}
        </span>
      )}
    </div>
  );
}

/** §33 per-step record ("Catat langkah"): status, owner, note and same-domain evidence ids, audited server-side.
 * Recording a step never deletes, revokes or contacts anyone. */
export function RetirementStepForm({ projectId, item, onDone }: { projectId: string; item: RetirementItem; onDone?: () => void }) {
  const qc = useQueryClient();
  const [status, setStatus] = useState(item.recorded?.status ?? "DONE");
  const [owner, setOwner] = useState(item.recorded?.owner ?? "");
  const [note, setNote] = useState(item.recorded?.note ?? "");
  const [evidence, setEvidence] = useState((item.recorded?.evidence_ids ?? []).join(", "));
  const save = useMutation({
    mutationFn: () =>
      put(`/api/ops/retirement/${encodeURIComponent(projectId)}/steps/${encodeURIComponent(item.key)}`, {
        status,
        owner: owner.trim(),
        note: note.trim(),
        evidence_ids: evidence.split(/[\s,]+/).filter(Boolean),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ops-retirement", projectId] });
      onDone?.();
    },
  });
  return (
    <div className="mt-2 grid gap-2 rounded-md border border-line p-2 text-sm">
      <label className="grid gap-1">
        <span className="text-xs text-ink-3">Status langkah</span>
        <select aria-label={`Status langkah ${item.key}`} className={inputClass} value={status} onChange={(e) => setStatus(e.target.value)}>
          {STEP_STATUSES.map(([v, , label]) => (
            <option key={v} value={v}>
              {label} ({v})
            </option>
          ))}
        </select>
      </label>
      <label className="grid gap-1">
        <span className="text-xs text-ink-3">Pemilik langkah (kosong = Anda)</span>
        <input aria-label={`Pemilik langkah ${item.key}`} className={inputClass} value={owner} maxLength={200} onChange={(e) => setOwner(e.target.value)} />
      </label>
      <label className="grid gap-1">
        <span className="text-xs text-ink-3">Catatan (apa yang dilakukan, kepada siapa)</span>
        <textarea aria-label={`Catatan langkah ${item.key}`} className={inputClass} value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} />
      </label>
      <label className="grid gap-1">
        <span className="text-xs text-ink-3">ID bukti (opsional, dipisah koma; domain proyek ini)</span>
        <input aria-label={`Bukti langkah ${item.key}`} className={inputClass} value={evidence} onChange={(e) => setEvidence(e.target.value)} />
      </label>
      <ErrorBox error={save.error} />
      <div>
        <Button variant="primary" busy={save.isPending} onClick={() => save.mutate()}>
          Simpan langkah
        </Button>
      </div>
      <div className="text-xs text-ink-3">Hanya mencatat langkah (diaudit); tidak ada yang dihapus, dicabut atau dikirim dari sini.</div>
    </div>
  );
}

function StepControl({ projectId, item }: { projectId: string; item: RetirementItem }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-1">
      <Button aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Tutup" : "Catat langkah"}
      </Button>
      {open && <RetirementStepForm projectId={projectId} item={item} onDone={() => setOpen(false)} />}
    </div>
  );
}

/** Retirement checklist (§33): maps what exists, which owner actions remain and what the owner recorded per
 * step. With ``canRecord`` (an admin session) each step offers "Catat langkah"; nothing is deleted from here. */
export function RetirementView({ data, canRecord = false }: { data: RetirementChecklist; canRecord?: boolean }) {
  return (
    <div className="space-y-3">
      <Notice tone="info">{data.notice}</Notice>
      {data.blocking.length > 0 && (
        <Notice tone="warn">
          Masih memblokir: {data.blocking.join(", ")}. Selesaikan butir ini sebelum keputusan RETIREMENT dicatat pemilik.
        </Notice>
      )}
      <ol className="space-y-2 text-sm" aria-label="Daftar periksa retirement">
        {data.items.map((i) => (
          <li key={i.key} className="rounded-md border border-line p-2">
            <div className="flex flex-wrap items-center gap-2">
              <OpsBadge status={i.status} />
              <span className="font-medium">{i.title}</span>
            </div>
            <div className="mt-1 text-ink-2">{i.owner_action}</div>
            {Object.keys(i.detail).length > 0 && <div className="mt-0.5 text-xs text-ink-3">{detailText(i.detail)}</div>}
            {i.recorded && <RecordedStep step={i.recorded} />}
            {canRecord && STEP_KEYS.includes(i.key) && <StepControl projectId={data.project.id} item={i} />}
          </li>
        ))}
      </ol>
      <Provenance
        source="projects, task_executions, capability_grants, publications, integrations, backups (read-only)"
        checkedAt={data.checked_at}
        evidenceLimit="Daftar periksa, bukan bukti penutupan; tidak ada yang dihapus atau dicabut dari halaman ini."
      />
    </div>
  );
}

/** §33 final verification by the legitimate owner: a RETIREMENT decision bound to the checklist hash shown. */
export function RetirementDecisionForm({ data, isAdmin }: { data: RetirementChecklist; isAdmin: boolean }) {
  const qc = useQueryClient();
  const [rationale, setRationale] = useState("");
  const record = useMutation({
    mutationFn: () =>
      post(`/api/ops/retirement/${encodeURIComponent(data.project.id)}/decision`, { rationale, checklist_hash: data.checklist_hash ?? null }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ops-retirement", data.project.id] }),
  });
  if (data.project.lifecycle !== "RETIRING") {
    return <Notice tone="info">Verifikasi akhir dicatat saat proyek berstatus RETIRING; RETIRED hanya setelah keputusan RETIREMENT pemilik.</Notice>;
  }
  return (
    <div className="grid gap-2 rounded-md border border-line p-2 text-sm">
      <div className="font-medium">Verifikasi akhir pemilik (keputusan RETIREMENT)</div>
      <textarea
        aria-label="Rationale verifikasi akhir"
        className={inputClass}
        value={rationale}
        onChange={(e) => setRationale(e.target.value)}
        placeholder="Apa yang sudah Anda periksa (min. 10 karakter)"
      />
      <ErrorBox error={record.error} />
      <div>
        <Button disabled={!isAdmin || data.blocking.length > 0 || rationale.trim().length < 10} busy={record.isPending} onClick={() => record.mutate()}>
          Catat verifikasi akhir
        </Button>
      </div>
      <div className="text-xs text-ink-3">Terikat hash daftar periksa ini; perubahan setelahnya membuat verifikasi STALE. Tidak ada yang dihapus.</div>
    </div>
  );
}

export function RetirementTab() {
  const { projectId, isAdmin } = useAuth();
  const q = useQuery({
    queryKey: ["ops-retirement", projectId],
    queryFn: () => get<RetirementChecklist>(`/api/ops/retirement/${encodeURIComponent(projectId ?? "")}`),
    enabled: !!projectId,
  });
  if (!projectId) {
    return (
      <EmptyState title="Belum memilih proyek">
        Pilih proyek di header untuk melihat daftar periksa retirement-nya (API: <Mono>GET /api/ops/retirement/&lt;project_id&gt;</Mono>).
      </EmptyState>
    );
  }
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  return q.data ? (
    <div className="space-y-3">
      <RetirementView data={q.data} canRecord={!!isAdmin} />
      <RetirementDecisionForm data={q.data} isAdmin={!!isAdmin} />
    </div>
  ) : null;
}
