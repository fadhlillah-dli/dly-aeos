import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Layers } from "lucide-react";
import { useState } from "react";
import { get, post } from "../../api/client";
import { StatusBadge } from "../StatusBadge";
import { Button, Card, ErrorBox, Field, Loading, Mono, Notice, inputClass } from "../ui";
import type { ConcurrencyState, ExperimentRow } from "./types";

const SOURCE_LABEL: Record<string, string> = {
  PILOT_DEFAULT: "default pilot (satu worker aktif)",
  POLICY_DECISION: "keputusan POLICY pemilik (workforce/concurrency)",
  EXPERIMENT_GRANT: "hibah eksperimen pemilik untuk satu EvalRun",
};
const RISKS = ["R0", "R1", "R2", "R3", "R4"];
const DOMAINS = ["PERSONAL", "OFFICE"];

function split(v: string, sep: RegExp): string[] {
  return v
    .split(sep)
    .map((x) => x.trim())
    .filter(Boolean);
}

function ExperimentGrant({ row, isAdmin, onDone }: { row: ExperimentRow; isAdmin: boolean; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [rationale, setRationale] = useState("");
  // like every EXCEPTION the grant expires on a date the owner chooses: no pre-filled value
  const [days, setDays] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (!row.grantable || row.grant_active) return null;
  const daysNum = Number(days);
  const daysOk = days !== "" && Number.isInteger(daysNum) && daysNum >= 1 && daysNum <= 30;
  if (!open) {
    return (
      <Button disabled={!isAdmin} title={isAdmin ? undefined : "Hanya SUPER_ADMIN; peran VIEWER hanya-baca"} onClick={() => setOpen(true)}>
        <FlaskConical className="h-4 w-4" aria-hidden /> Beri hibah eksperimen 2 worker…
      </Button>
    );
  }
  return (
    <div role="dialog" aria-label={`Hibah eksperimen ${row.eval_run_id}`} className="mt-2 space-y-2 rounded-md border border-line bg-surface-2 p-3">
      <Notice tone="info">
        Keputusan yang akan dicatat: <b>EXCEPTION</b> oleh Anda (manusia), terikat ke EvalRun <Mono>{row.eval_run_id}</Mono> yang sedang
        RUNNING (topologi C) dan waktu mulainya. Dua worker hanya untuk TaskSpec yang terikat ke slot run ini, selama run masih RUNNING dan
        tidak ada pekerjaan lain yang aktif. Konflik write-set, reservasi compute, budget, sandbox, route dan verifier tetap diperiksa.
      </Notice>
      <Field label="Alasan (wajib, min 10 karakter)">
        <textarea className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
      </Field>
      <Field label="Kedaluwarsa (hari, 1-30, wajib)" hint="Seperti setiap EXCEPTION, hibah berakhir pada tanggal yang Anda pilih (atau lebih awal saat run berhenti).">
        <input className={inputClass} inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value.replace(/[^0-9]/g, ""))} />
      </Field>
      <ErrorBox error={error} />
      <div className="flex gap-2">
        <Button
          variant="primary"
          busy={busy}
          disabled={rationale.trim().length < 10 || !daysOk}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              await post("/api/workforce/concurrency/experiment", { eval_run_id: row.eval_run_id, rationale: rationale.trim(), days: daysNum });
              setOpen(false);
              onDone();
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          Catat hibah
        </Button>
        <Button variant="ghost" onClick={() => setOpen(false)}>
          Batal
        </Button>
      </div>
    </div>
  );
}

function ModeForm({ isAdmin, onDone }: { isAdmin: boolean; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<1 | 2>(2);
  const [rationale, setRationale] = useState("");
  const [evalRun, setEvalRun] = useState("");
  const [baseline, setBaseline] = useState("");
  const [roles, setRoles] = useState("");
  const [risks, setRisks] = useState<string[]>([]);
  const [domains, setDomains] = useState<string[]>([]);
  const [projects, setProjects] = useState("");
  const [options, setOptions] = useState("");
  const [goal, setGoal] = useState("");
  const [tradeoff, setTradeoff] = useState("");
  const [invalidateIf, setInvalidateIf] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<string | null>(null);
  const toggle = (list: string[], set: (v: string[]) => void, x: string) => set(list.includes(x) ? list.filter((y) => y !== x) : [...list, x]);
  if (!open) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <Button disabled={!isAdmin} title={isAdmin ? undefined : "Hanya SUPER_ADMIN; peran VIEWER hanya-baca"} onClick={() => setOpen(true)}>
          <Layers className="h-4 w-4" aria-hidden /> Ubah mode konkurensi…
        </Button>
        {result && <span className="text-xs text-ink-2">{result}</span>}
      </div>
    );
  }
  const ready =
    mode === 1 ||
    (rationale.trim().length >= 10 && evalRun.trim() && baseline.trim() && split(options, /\n/).length > 0 && goal.trim() && tradeoff.trim() && invalidateIf.trim());
  return (
    <div role="dialog" aria-label="Mode konkurensi" className="space-y-2 rounded-md border border-line bg-surface-2 p-3">
      <Field label="Mode">
        <select className={inputClass} value={mode} onChange={(e) => setMode(Number(e.target.value) as 1 | 2)}>
          <option value={1}>1 worker - mode pilot (rollback, tanpa keputusan)</option>
          <option value={2}>2 worker - perlu manfaat terbukti topologi C vs A</option>
        </select>
      </Field>
      {mode === 2 && (
        <>
          <Notice tone="warn">
            Dua worker hanya untuk kelas tugas yang terbukti (§44): EvalRun topologi C yang COMPLETED, semua gate PASS, benar-benar menjalankan dua
            worker bersamaan, dan lebih baik dari baseline topologi A pada suite/konfigurasi/kondisi yang sama. Scope dibatasi ke peran yang diukur
            suite tersebut. Keputusan POLICY dicatat atas nama Anda.
          </Notice>
          <Field label="ID EvalRun topologi C (COMPLETED)">
            <input className={inputClass} value={evalRun} onChange={(e) => setEvalRun(e.target.value)} />
          </Field>
          <Field label="ID EvalRun baseline topologi A (COMPLETED)">
            <input className={inputClass} value={baseline} onChange={(e) => setBaseline(e.target.value)} />
          </Field>
          <fieldset className="space-y-2 rounded border border-line p-2">
            <legend className="px-1 text-sm font-medium">Scope kelas tugas (wajib salah satu)</legend>
            <Field label="Role key" hint="Harus peran yang diukur suite; kosong = diisi otomatis dari suite.">
              <input className={inputClass} value={roles} onChange={(e) => setRoles(e.target.value)} />
            </Field>
            <div className="flex flex-wrap gap-3 text-sm">
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
        </>
      )}
      <Field label={mode === 2 ? "Alasan (wajib, min 10 karakter)" : "Alasan (dicatat di audit)"}>
        <textarea className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
      </Field>
      <ErrorBox error={error} />
      <div className="flex gap-2">
        <Button
          variant="primary"
          busy={busy}
          disabled={!ready}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              const scope: Record<string, string[]> = {};
              if (split(roles, /[\s,]+/).length) scope.role_keys = split(roles, /[\s,]+/);
              if (risks.length) scope.risk_classes = risks;
              if (domains.length) scope.domains = domains;
              if (split(projects, /[\s,]+/).length) scope.project_ids = split(projects, /[\s,]+/);
              const out = await post<{ effective_cap: number; cap_reason: string }>("/api/workforce/concurrency", {
                max_active: mode,
                rationale: rationale.trim(),
                ...(mode === 2
                  ? {
                      eval_run_id: evalRun.trim(),
                      baseline_eval_run_id: baseline.trim(),
                      scope,
                      decision_record: { options: split(options, /\n/), goal: goal.trim(), cost_quality_tradeoff: tradeoff.trim(), invalidate_if: invalidateIf.trim() },
                    }
                  : {}),
              });
              setResult(`Batas efektif sekarang ${out.effective_cap}: ${out.cap_reason}`);
              setOpen(false);
              onDone();
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          Catat
        </Button>
        <Button variant="ghost" onClick={() => setOpen(false)}>
          Batal
        </Button>
      </div>
    </div>
  );
}

/** §19/§21/§44/§51 P2: the effective worker cap, its source, the owner's resource budget, the two-worker experiments. */
export function ConcurrencyPanel({ domain, isAdmin }: { domain: string | null; isAdmin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["workforce-concurrency", domain], queryFn: () => get<ConcurrencyState>("/api/workforce/concurrency"), refetchInterval: 30_000 });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["workforce-concurrency", domain] });
    qc.invalidateQueries({ queryKey: ["workforce-summary", domain] });
  };
  return (
    <Card title="Konkurensi worker (§19/§44)">
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : q.data ? (
        <div className="space-y-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={q.data.concurrency.cap > 1 ? "ENABLED" : "CONFIGURED"} label={`Batas efektif: ${q.data.concurrency.cap} worker`} />
            <span className="text-xs text-ink-2">Sumber: {SOURCE_LABEL[q.data.concurrency.source] ?? q.data.concurrency.source}</span>
          </div>
          <p className="text-xs text-ink-3">{q.data.concurrency.reason}</p>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {q.data.concurrency.budget_max_parallel_workers === null ? (
              <StatusBadge status="UNKNOWN" label="Budget resource: belum ditetapkan pemilik" />
            ) : (
              <StatusBadge status="CONFIGURED" label={`Budget resource: maks ${q.data.concurrency.budget_max_parallel_workers} worker paralel`} />
            )}
            <span className="text-ink-3">{q.data.concurrency.budget_note}</span>
          </div>
          {q.data.concurrency.budget_max_parallel_workers === null && (
            <p className="text-xs text-ink-3">
              Tetapkan dari mesin nyata: <Mono>aeos doctor budget --max-workers &lt;n&gt; --reason &lt;teks&gt;</Mono> (tidak pernah ditebak).
            </p>
          )}
          <ModeForm isAdmin={isAdmin} onDone={refresh} />
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-3">Eksperimen bounded-parallel (EvalRun topologi C)</h3>
            {q.data.items.length === 0 ? (
              <p className="text-xs text-ink-3">
                Belum ada EvalRun topologi C yang RUNNING/COMPLETED di domain ini. Rencanakan di halaman Evaluasi, mulai dengan{" "}
                <Mono>aeos eval start &lt;run_id&gt; --as &lt;admin&gt;</Mono>, lalu ikat task ke slot (<Mono>aeos eval bind … [--append]</Mono>).
              </p>
            ) : (
              <ul className="mt-1 space-y-2">
                {q.data.items.map((row) => (
                  <li key={row.eval_run_id} className="rounded-md border border-line p-2 text-xs">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Mono>{row.eval_run_id}</Mono>
                      <StatusBadge status={row.status} />
                      {row.grant_active ? (
                        <StatusBadge status="APPROVED" label="Hibah 2 worker aktif" />
                      ) : row.grant_decision_id ? (
                        <StatusBadge status="EXPIRED" label="Hibah tercatat, tidak berlaku lagi (1 worker)" />
                      ) : (
                        <StatusBadge status="NOT_RECORDED" label="Tanpa hibah (1 worker)" />
                      )}
                      <StatusBadge
                        status={row.parallel_execution.status === "PROVEN" ? "VERIFIED" : "NOT_PROVEN"}
                        label={row.parallel_execution.status === "PROVEN" ? "Dua worker bersamaan: terbukti" : "Dua worker bersamaan: belum terbukti"}
                      />
                      <span className="text-ink-3">{row.bound_tasks} task terikat</span>
                    </div>
                    {row.parallel_execution.problems.length > 0 && (
                      <ul className="mt-1 list-disc pl-5 text-ink-3">
                        {row.parallel_execution.problems.map((p) => (
                          <li key={p}>{p}</li>
                        ))}
                      </ul>
                    )}
                    {row.problems.length > 0 && <p className="mt-1 text-ink-3">{row.problems.join("; ")}</p>}
                    <ExperimentGrant row={row} isAdmin={isAdmin} onDone={refresh} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      ) : null}
    </Card>
  );
}
