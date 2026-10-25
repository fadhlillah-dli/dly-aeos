import { Stamp } from "lucide-react";
import { useState } from "react";
import { post } from "../../api/client";
import { StatusBadge } from "../StatusBadge";
import { Button, ErrorBox, Field, Mono, Notice, inputClass } from "../ui";
import type { RoleCard } from "./types";

const RISKS = ["R0", "R1", "R2", "R3", "R4"];
const DOMAINS = ["PERSONAL", "OFFICE"];

function lines(v: string): string[] {
  return v
    .split(/\n/)
    .map((x) => x.trim())
    .filter(Boolean);
}

/**
 * §44 "Hasil routing/role/mode menjadi DecisionRecord", §46 eligible from evaluation results: the owner adopts the
 * role's CURRENT PASS evaluation (bound to the role revision and the EvalRun) with the DecisionRecord fields and an
 * optional scope. A PASS result is never eligibility by itself; nothing here runs a model.
 */
export function RoleAdoption({ role, isAdmin, onChanged }: { role: RoleCard; isAdmin: boolean; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [rationale, setRationale] = useState("");
  const [options, setOptions] = useState("");
  const [goal, setGoal] = useState("");
  const [tradeoff, setTradeoff] = useState("");
  const [invalidateIf, setInvalidateIf] = useState("");
  const [risks, setRisks] = useState<string[]>([]);
  const [domains, setDomains] = useState<string[]>([]);
  const [projects, setProjects] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const ev = role.evaluation;
  if (!ev) return null;
  const adoption = role.adoption;
  const toggle = (list: string[], set: (v: string[]) => void, x: string) => set(list.includes(x) ? list.filter((y) => y !== x) : [...list, x]);
  const filled = Boolean(rationale.trim().length >= 5 && lines(options).length > 0 && goal.trim() && tradeoff.trim() && invalidateIf.trim());
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const scope: Record<string, string[]> = {};
      if (risks.length) scope.risk_classes = risks;
      if (domains.length) scope.domains = domains;
      const pids = projects.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
      if (pids.length) scope.project_ids = pids;
      await post(`/api/workforce/roles/${encodeURIComponent(role.role_key)}/adopt`, {
        eval_run_id: ev.run_id,
        rationale: rationale.trim(),
        decision_record: { options: lines(options), goal: goal.trim(), cost_quality_tradeoff: tradeoff.trim(), invalidate_if: invalidateIf.trim() },
        scope,
      });
      setOpen(false);
      onChanged();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-1.5 rounded-md border border-line p-2 text-xs" data-testid={`adoption-${role.role_key}`}>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-ink-3">Hasil evaluasi revisi ini:</span>
        <StatusBadge status={ev.status === "NOT_EVALUATED" ? "NOT_TESTED" : ev.status} label={ev.status === "NOT_EVALUATED" ? "Belum dievaluasi" : undefined} />
        {adoption?.current ? (
          <StatusBadge status="APPROVED" label="Hasil diadopsi pemilik" />
        ) : (
          <StatusBadge status="NOT_RECORDED" label="Belum diadopsi" />
        )}
      </div>
      <p className="text-ink-3">{ev.basis}</p>
      {adoption?.current && adoption.decision_id && (
        <p>
          Keputusan adopsi <Mono>{adoption.decision_id}</Mono> untuk EvalRun <Mono>{adoption.eval_run_id ?? "—"}</Mono>
          {adoption.scope && Object.keys(adoption.scope).length ? ` · scope ${JSON.stringify(adoption.scope)}` : " · semua task peran ini"}
        </p>
      )}
      {role.adoptable && !open && (
        <Button variant="secondary" disabled={!isAdmin} title={isAdmin ? undefined : "Hanya SUPER_ADMIN; peran VIEWER hanya-baca"} onClick={() => setOpen(true)}>
          <Stamp className="h-4 w-4" aria-hidden /> Adopsi hasil evaluasi…
        </Button>
      )}
      {role.adoptable && open && (
        <div role="dialog" aria-label={`Adopsi hasil evaluasi ${role.name}`} className="space-y-2 rounded-md border border-line bg-surface-2 p-3">
          <Notice tone="info">
            Keputusan yang akan dicatat: <b>POLICY role_adoption</b> oleh Anda (manusia), terikat ke revisi peran{" "}
            <Mono>{(role.revision_hash ?? "UNKNOWN").slice(0, 19)}</Mono> dan EvalRun <Mono>{ev.run_id}</Mono>. Revisi peran baru atau hasil
            evaluasi yang lebih baru membuat keputusan ini tidak berlaku; mencabutnya mengembalikan aturan ambang/EXCEPTION.
          </Notice>
          <fieldset className="space-y-2 rounded border border-line p-2">
            <legend className="px-1 text-sm font-medium">Catatan keputusan (DecisionRecord §44, wajib)</legend>
            <Field label="Opsi yang dipertimbangkan" hint="Satu opsi per baris.">
              <textarea className={inputClass} rows={2} value={options} onChange={(e) => setOptions(e.target.value)} />
            </Field>
            <Field label="Tujuan">
              <input className={inputClass} value={goal} onChange={(e) => setGoal(e.target.value)} />
            </Field>
            <Field label="Trade-off biaya/kualitas">
              <input className={inputClass} value={tradeoff} onChange={(e) => setTradeoff(e.target.value)} />
            </Field>
            <Field label="Batal berlaku bila">
              <input className={inputClass} value={invalidateIf} onChange={(e) => setInvalidateIf(e.target.value)} />
            </Field>
          </fieldset>
          <fieldset className="rounded border border-line p-2">
            <legend className="px-1 text-sm font-medium">Scope (opsional; kosong = semua task peran ini)</legend>
            <div className="flex flex-wrap gap-3">
              {RISKS.map((r) => (
                <label key={r} className="inline-flex items-center gap-1">
                  <input type="checkbox" checked={risks.includes(r)} onChange={() => toggle(risks, setRisks, r)} /> {r}
                </label>
              ))}
              {DOMAINS.map((d) => (
                <label key={d} className="inline-flex items-center gap-1">
                  <input type="checkbox" checked={domains.includes(d)} onChange={() => toggle(domains, setDomains, d)} /> {d}
                </label>
              ))}
            </div>
            <Field label="ID proyek (opsional)" hint="Pisahkan dengan koma.">
              <input className={inputClass} value={projects} onChange={(e) => setProjects(e.target.value)} />
            </Field>
          </fieldset>
          <Field label="Alasan (wajib, min 5 karakter)">
            <textarea className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
          </Field>
          <ErrorBox error={error} />
          <div className="flex gap-2">
            <Button variant="primary" busy={busy} disabled={!filled} onClick={submit}>
              Catat adopsi
            </Button>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Batal
            </Button>
          </div>
        </div>
      )}
      {!role.adoptable && !adoption?.current && (
        <p className="text-ink-3">Belum ada hasil PASS revisi ini yang dapat diadopsi; admission memakai ambang risiko / EXCEPTION pemilik.</p>
      )}
    </div>
  );
}
