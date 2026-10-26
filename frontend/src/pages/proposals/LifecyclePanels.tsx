import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link2, Play, Plus, Square, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { patch, post } from "../../api/client";
import { MappedBadge, PROPOSAL_STATUS } from "../../components/proposals/MappedBadge";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, ErrorBox, Field, inputClass, KeyValue, Mono, Notice, Table } from "../../components/ui";
import { absoluteTime } from "../../lib/format";
import type { AcceptanceCriterion, ProposalDetail, RemovalItem } from "./types";

const lines = (s: string) =>
  s
    .split(/[\n,]/)
    .map((x) => x.trim())
    .filter(Boolean);

function useProposalMutation<T>(id: string, fn: (body: T) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["proposal", id] });
    },
  });
}

type Draft = Omit<AcceptanceCriterion, "id" | "suite"> & { id?: string; suite?: string | null };

/** §40 "Keberhasilan trial ditetapkan sebelum eksekusi" + §40 replacement plan: bound to the candidate hash. */
export function OwnerPlanCard({ p, isAdmin }: { p: ProposalDetail; isAdmin: boolean }) {
  const plan = p.owner_plan ?? {};
  const [rows, setRows] = useState<Draft[]>(
    plan.acceptance?.length ? plan.acceptance : [{ criterion: "", metric: "", threshold: "", baseline_value: "", required: true }],
  );
  const [owner, setOwner] = useState(plan.operations_owner ?? "");
  const rp = plan.replacement ?? {};
  const [cutover, setCutover] = useState(rp.cutover_order ?? "");
  const [retire, setRetire] = useState(rp.retire_old_version_after ?? "");
  const [recovery, setRecovery] = useState(rp.rollback_or_forward_recovery ?? "");
  const [compat, setCompat] = useState(rp.version_schema_data_compatibility ?? "");
  const [restore, setRestore] = useState(rp.backup_restore_evidence_id ?? "");
  const [coexist, setCoexist] = useState<string>(rp.coexistence_safe === undefined ? "" : String(rp.coexistence_safe));
  const replacing = ["REPLACE", "UPGRADE", "REMOVE"].includes(p.kind);
  const m = useProposalMutation(p.id, (body: Record<string, unknown>) => patch(`/api/proposals/${p.id}/plan`, body));
  const locked = p.status === "PROMOTION_APPROVED" || ["ACCEPTED", "ROLLED_BACK", "FAILED", "PARTIAL"].includes(p.status);
  if (p.kind === "COMPARE") return null;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const body: Record<string, unknown> = {
      acceptance: rows.filter((r) => r.criterion.trim()).map((r) => ({ ...r, suite: r.suite || null })),
      operations_owner: owner,
    };
    if (replacing) {
      const repl: Record<string, unknown> = {
        cutover_order: cutover,
        retire_old_version_after: retire,
        rollback_or_forward_recovery: recovery,
        version_schema_data_compatibility: compat,
      };
      if (restore) repl.backup_restore_evidence_id = restore;
      if (coexist) repl.coexistence_safe = coexist === "true";
      body.replacement = repl;
    }
    m.mutate(body);
  };
  return (
    <Card title="Rencana pemilik (ditetapkan sebelum trial, terikat candidate hash)">
      <Notice>
        Acceptance terukur (metrik + ambang), pemilik operasi dan rencana cutover/rollback adalah bagian dari yang disetujui: mengubahnya membuat binding keputusan harus diulang.
      </Notice>
      {plan.acceptance?.length ? (
        <Table
          rows={plan.acceptance}
          rowKey={(a) => a.id}
          columns={[
            { header: "ID", cell: (a) => <Mono>{a.id}</Mono> },
            { header: "Kriteria", cell: (a) => a.criterion },
            { header: "Metrik / ambang", cell: (a) => `${a.metric || "—"} ${a.threshold || ""}` },
            { header: "Baseline", cell: (a) => a.baseline_value },
            { header: "Wajib", cell: (a) => (a.required ? "Ya" : "Tidak") },
          ]}
        />
      ) : (
        <p className="text-sm text-ink-3">Belum ada acceptance yang ditetapkan — TRIAL_APPROVE diblokir (manfaat tidak dapat diuji).</p>
      )}
      {isAdmin && !locked && (
        <form onSubmit={submit} className="mt-3 space-y-3" aria-label="Rencana pemilik">
          {rows.map((r, i) => (
            <div key={i} className="grid gap-2 sm:grid-cols-[2fr_1fr_1fr_1fr_auto] sm:items-end">
              <Field label={`Kriteria ${i + 1}`}>
                <input className={inputClass} value={r.criterion} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, criterion: e.target.value } : x)))} />
              </Field>
              <Field label="Metrik">
                <input className={inputClass} value={r.metric} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, metric: e.target.value } : x)))} />
              </Field>
              <Field label="Ambang">
                <input className={inputClass} value={r.threshold} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, threshold: e.target.value } : x)))} />
              </Field>
              <Field label="Baseline">
                <input className={inputClass} value={r.baseline_value} onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, baseline_value: e.target.value } : x)))} />
              </Field>
              <Button type="button" variant="ghost" aria-label={`Hapus kriteria ${i + 1}`} onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                <Trash2 className="h-4 w-4" aria-hidden />
              </Button>
            </div>
          ))}
          <Button type="button" onClick={() => setRows([...rows, { criterion: "", metric: "", threshold: "", baseline_value: "", required: true }])}>
            <Plus className="h-4 w-4" aria-hidden /> Tambah kriteria
          </Button>
          <Field label="Pemilik operasi hasil" hint="Wajib sebelum PROMOTE">
            <input className={inputClass} value={owner} onChange={(e) => setOwner(e.target.value)} />
          </Field>
          {replacing && (
            <div className="grid gap-3 md:grid-cols-2">
              <Field label="Urutan cutover">
                <textarea className={inputClass} rows={2} value={cutover} onChange={(e) => setCutover(e.target.value)} />
              </Field>
              <Field label="Kapan versi lama boleh dihentikan">
                <input className={inputClass} value={retire} onChange={(e) => setRetire(e.target.value)} />
              </Field>
              <Field label="Rollback / forward recovery">
                <textarea className={inputClass} rows={2} value={recovery} onChange={(e) => setRecovery(e.target.value)} />
              </Field>
              <Field label="Kompatibilitas versi/schema/data">
                <textarea className={inputClass} rows={2} value={compat} onChange={(e) => setCompat(e.target.value)} />
              </Field>
              <Field label="ID bukti restore teruji" hint="aeos ops backup restore-test; wajib bila ada state/data">
                <input className={inputClass} value={restore} onChange={(e) => setRestore(e.target.value)} />
              </Field>
              <Field label="Coexistence aman?">
                <select className={inputClass} value={coexist} onChange={(e) => setCoexist(e.target.value)}>
                  <option value="">UNKNOWN</option>
                  <option value="true">Ya</option>
                  <option value="false">Tidak — cutover berizin</option>
                </select>
              </Field>
            </div>
          )}
          <ErrorBox error={m.error} />
          <Button type="submit" variant="primary" busy={m.isPending}>
            Simpan rencana (review ulang otomatis)
          </Button>
        </form>
      )}
      {locked && <p className="mt-2 text-xs text-ink-3">Rencana terkunci: promosi sudah disetujui untuk rencana ini atau hasil sudah tercatat.</p>}
    </Card>
  );
}

/** §39 REMOVE closure (planKey "removal": every item CLOSED or WAIVED before ACCEPTED) and §40 credential revocation
 * of a REPLACE/UPGRADE (planKey "credentials": progress per secret ref, not a gate). Recording never revokes or deletes
 * anything; the owner does that at the provider and with 'aeos secrets delete'. */
export function RemovalClosure({
  p,
  items,
  isAdmin,
  planKey = "removal",
}: {
  p: ProposalDetail;
  items: RemovalItem[];
  isAdmin: boolean;
  planKey?: "removal" | "credentials";
}) {
  const m = useProposalMutation(p.id, (body: Record<string, unknown>) => patch(`/api/proposals/${p.id}/plan`, body));
  const [notes, setNotes] = useState<Record<string, string>>({});
  const mark = (id: string, status: string) => m.mutate({ [planKey]: { [id]: { status, note: notes[id] ?? "" } } });
  return (
    <div className="mt-3">
      <Table
        rows={items}
        rowKey={(i) => i.id}
        columns={[
          { header: "Jenis", cell: (i) => <Mono>{i.kind}</Mono> },
          {
            header: planKey === "credentials" ? "Yang harus dicabut" : "Yang harus ditutup",
            cell: (i) => (
              <span>
                {i.item}
                {i.shared_with && i.shared_with.length > 0 && (
                  <span className="ml-1">
                    <StatusBadge status="WARN" label="SHARED (masih dipakai)" />
                  </span>
                )}
              </span>
            ),
          },
          { header: "Status", cell: (i) => <StatusBadge status={i.status === "CLOSED" ? "DONE" : i.status === "WAIVED" ? "NOT_APPLICABLE" : "PENDING"} label={i.status} /> },
          {
            header: "Aksi",
            cell: (i) =>
              isAdmin && i.status === "OPEN" ? (
                <div className="flex flex-wrap items-center gap-1">
                  <input aria-label={`Catatan ${i.id}`} className={inputClass + " w-40"} placeholder="catatan" value={notes[i.id] ?? ""} onChange={(e) => setNotes({ ...notes, [i.id]: e.target.value })} />
                  <Button className="text-xs" onClick={() => mark(i.id, "CLOSED")}>Tutup</Button>
                  <Button className="text-xs" onClick={() => mark(i.id, "WAIVED")}>Waive (alasan)</Button>
                </div>
              ) : (
                <span className="text-xs">{i.note}</span>
              ),
          },
        ]}
      />
      <ErrorBox error={m.error} />
    </div>
  );
}

/** §40 isolated trial under the TRIAL decision: launch (Plan/TaskSpec in the platform/project), stop/kill. */
export function TrialCard({ p, isAdmin }: { p: ProposalDetail; isAdmin: boolean }) {
  const t = p.trial ?? {};
  const [paths, setPaths] = useState("");
  const [reason, setReason] = useState("");
  const launch = useProposalMutation(p.id, () => post(`/api/proposals/${p.id}/trial/launch`, { allowed_paths: lines(paths) }));
  const stop = useProposalMutation(p.id, () => post(`/api/proposals/${p.id}/trial/stop`, { reason }));
  if (p.kind === "COMPARE") return null;
  const canLaunch = isAdmin && ["TRIAL_APPROVED", "IN_TRIAL"].includes(p.status) && t.status !== "LAUNCHED";
  return (
    <Card title="Trial terisolasi (di bawah keputusan TRIAL)">
      {t.status ? (
        <KeyValue
          items={[
            ["Status trial", <StatusBadge key="s" status={t.status === "LAUNCHED" ? "RUNNING" : "CANCELLED"} label={t.status} />],
            ["Plan", t.plan_id ? <Link to={`/plans/${t.plan_id}`}>{t.plan_id.slice(0, 8)}</Link> : "—"],
            ["TaskSpec", (t.task_ids ?? []).map((x) => <Mono key={x}>{x.slice(0, 8)}</Mono>)],
            ["Environment", t.environment ?? "—"],
            ["Batas", <Mono key="c">{JSON.stringify(t.caps ?? {})}</Mono>],
            ["Eksekusi", t.execution ? `${t.execution.status}${t.execution.reasons?.length ? ` — ${t.execution.reasons.join("; ")}` : ""}` : "—"],
            ["Diluncurkan", t.launched_at ? absoluteTime(t.launched_at) : "—"],
            ["Dihentikan", t.stopped_at ? `${absoluteTime(t.stopped_at)} — ${t.stop_reason ?? ""}` : "—"],
          ]}
        />
      ) : (
        <p className="text-sm text-ink-3">Belum diluncurkan. Trial hanya berjalan di sandbox/staging sesuai scope keputusan; scheduler memeriksa binding sebelum setiap attempt.</p>
      )}
      {t.execution?.note && <Notice tone="warn">{t.execution.note}</Notice>}
      {canLaunch && (
        <div className="mt-3 flex flex-wrap items-end gap-2">
          <Field label="Allowed paths (opsional)" hint="Default: sandbox/trials/<id>/**">
            <input className={inputClass + " w-72"} value={paths} onChange={(e) => setPaths(e.target.value)} />
          </Field>
          <Button variant="primary" onClick={() => launch.mutate(undefined)} busy={launch.isPending}>
            <Play className="h-4 w-4" aria-hidden /> Luncurkan trial
          </Button>
        </div>
      )}
      {isAdmin && t.status === "LAUNCHED" && (
        <div className="mt-3 flex flex-wrap items-end gap-2">
          <Field label="Alasan stop">
            <input className={inputClass + " w-72"} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
          <Button onClick={() => stop.mutate(undefined)} busy={stop.isPending} disabled={reason.trim().length < 3}>
            <Square className="h-4 w-4" aria-hidden /> Hentikan trial (stop/kill)
          </Button>
        </div>
      )}
      <ErrorBox error={launch.error ?? stop.error} />
    </Card>
  );
}

/** §40/§41: TaskSpec, eval run, publication, release, ADR and observation records - validated per domain. */
export function LinksCard({ p, isAdmin }: { p: ProposalDetail; isAdmin: boolean }) {
  const l = p.links;
  const [kind, setKind] = useState("task_ids");
  const [ids, setIds] = useState("");
  const m = useProposalMutation(p.id, () => post(`/api/proposals/${p.id}/links`, { [kind]: lines(ids) }));
  return (
    <Card title="Catatan tertaut (task, evaluasi, publikasi, rilis, ADR, observasi)">
      <KeyValue
        items={[
          ["TaskSpec", l?.tasks.length ? l.tasks.map((t) => <div key={t.id}><Mono>{t.task_key}</Mono> {t.title} — {t.state}</div>) : "—"],
          ["Publikasi", l?.publications.length ? l.publications.map((x) => <div key={x.id}><Mono>{x.id.slice(0, 8)}</Mono> {x.status}</div>) : "—"],
          ["Eval run", l?.eval_run_ids.length ? l.eval_run_ids.map((x) => <Mono key={x}>{x.slice(0, 8)}</Mono>) : "—"],
          ["Rilis controller", l?.release_ids.length ? l.release_ids.join(", ") : "—"],
          ["ADR", l?.adr_keys.length ? l.adr_keys.join(", ") : "—"],
          ["Observasi", l?.observation_ids.length ? l.observation_ids.map((x) => <Mono key={x}>{x.slice(0, 8)}</Mono>) : "—"],
        ]}
      />
      {isAdmin && (
        <form
          className="mt-3 flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            m.mutate(undefined);
          }}
        >
          <Field label="Jenis">
            <select className={inputClass} value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="task_ids">TaskSpec</option>
              <option value="eval_run_ids">Eval run</option>
              <option value="publication_ids">Publikasi</option>
              <option value="release_ids">Rilis controller</option>
              <option value="adr_keys">ADR</option>
              <option value="observation_ids">Observasi</option>
            </select>
          </Field>
          <Field label="ID (koma/baris)">
            <input className={inputClass + " w-72"} value={ids} onChange={(e) => setIds(e.target.value)} required />
          </Field>
          <Button type="submit" busy={m.isPending}>
            <Link2 className="h-4 w-4" aria-hidden /> Tautkan
          </Button>
          <ErrorBox error={m.error} />
        </form>
      )}
    </Card>
  );
}

/** §39/§40 "Claude review bukti" before PROMOTE: Claude bridge, manual result, or explicit NOT-PROVEN. */
export function CheckpointCard({ p, isAdmin }: { p: ProposalDetail; isAdmin: boolean }) {
  const cp = p.checkpoint_review;
  const [manual, setManual] = useState("");
  const [rationale, setRationale] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const m = useProposalMutation(p.id, (body: Record<string, unknown>) => post(`/api/proposals/${p.id}/checkpoint`, body));
  if (p.kind === "COMPARE") return null;
  const active = ["TRIAL_APPROVED", "IN_TRIAL", "PROMOTION_PENDING"].includes(p.status);
  return (
    <Card title="Checkpoint Claude atas bukti trial (sebelum PROMOTE)">
      {cp ? (
        <div className="space-y-2 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={cp.status === "COMPLETED" ? "CONFIGURED" : cp.status === "QUOTA_WAIT" ? "DEGRADED" : cp.status === "NOT_PROVEN_ACKNOWLEDGED" ? "NOT-PROVEN" : "PENDING"} label={cp.status} />
            {cp.verdict && <StatusBadge status={cp.verdict} label={`Verdict (data): ${cp.verdict}`} />}
            {cp.mode && <span className="text-xs text-ink-3">mode {cp.mode}</span>}
            {cp.gate && cp.gate !== "OK" && <StatusBadge status="BLOCKED" label={`Gate: ${cp.gate}`} />}
          </div>
          <p className="text-xs text-ink-3">{cp.label}</p>
          {cp.review?.summary && <p>REVIEW: {cp.review.summary}</p>}
          {cp.reason && <p className="text-ink-2">{cp.reason}</p>}
          {cp.gate_note && <p className="text-xs text-warn">{cp.gate_note}</p>}
          {cp.package && (
            <details>
              <summary className="cursor-pointer text-xs">Paket manual untuk Claude Code resmi</summary>
              <Mono>{JSON.stringify(cp.package, null, 2)}</Mono>
            </details>
          )}
        </div>
      ) : (
        <p className="text-sm text-ink-3">Belum ada checkpoint. PROMOTE memerlukan review bukti yang terikat candidate hash dan set bukti saat ini.</p>
      )}
      {isAdmin && active && (
        <div className="mt-3 space-y-2">
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => m.mutate({ with_claude: true })} busy={m.isPending} disabled={p.domain === "OFFICE"}>
              Minta checkpoint Claude{p.domain === "OFFICE" ? " (tidak untuk OFFICE)" : ""}
            </Button>
            <Button onClick={() => m.mutate({})} busy={m.isPending}>
              Buat paket manual
            </Button>
          </div>
          <Field label="Hasil manual (JSON REVIEW/VERIFY/VERDICT dari Claude Code resmi)">
            <textarea className={inputClass} rows={3} value={manual} onChange={(e) => setManual(e.target.value)} />
          </Field>
          <Button
            onClick={() => {
              setParseError(null);
              try {
                m.mutate({ manual_result: JSON.parse(manual) as Record<string, unknown> });
              } catch {
                setParseError("Hasil manual harus JSON yang valid");
              }
            }}
            disabled={!manual.trim()}
          >
            Kirim hasil manual
          </Button>
          <Field label="Alasan mencatat NOT-PROVEN (hanya bila Claude tidak dapat dijalankan)">
            <input className={inputClass} value={rationale} onChange={(e) => setRationale(e.target.value)} />
          </Field>
          <Button onClick={() => m.mutate({ acknowledge_not_proven: true, rationale })} disabled={rationale.trim().length < 10}>
            Catat NOT-PROVEN secara eksplisit
          </Button>
          {parseError && <ErrorBox error={new Error(parseError)} />}
          <ErrorBox error={m.error} />
        </div>
      )}
    </Card>
  );
}

export function PossibleDuplicates({ p }: { p: ProposalDetail }) {
  const rows = p.possible_duplicates ?? [];
  if (!rows.length) return null;
  return (
    <Notice tone="warn">
      <div className="mb-1 font-medium">Kemungkinan duplikat (tujuan/komponen sama) — saran saja, tidak digabung otomatis:</div>
      <ul className="list-disc pl-5">
        {rows.map((d) => (
          <li key={d.id}>
            <Link to={`/proposals/${d.id}`}>{d.title}</Link> <MappedBadge value={d.status} map={PROPOSAL_STATUS} /> — {d.reason}
          </li>
        ))}
      </ul>
    </Notice>
  );
}
