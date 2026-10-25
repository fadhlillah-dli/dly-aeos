import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Microscope } from "lucide-react";
import { useState } from "react";
import { get, post } from "../../api/client";
import { StatusBadge } from "../StatusBadge";
import { Button, Card, ErrorBox, Field, Loading, Mono, Notice, inputClass } from "../ui";

interface Hypothesis {
  key: string;
  statement: string;
  index: number;
  investigated: boolean;
  run_id: string | null;
  outcome_code: string | null;
  verdict: string | null;
  evidenced: boolean;
  evidence_refs: { ref: string; exists: boolean }[];
  summary: string | null;
  proposed_fix: string | null;
}

interface InvestigationView {
  task_id: string;
  task_key: string;
  state: string | null;
  reason_code: string | null;
  repair_task_key: string | null;
  timebox_seconds: number | null;
  hypotheses: Hypothesis[];
  evidenced: string[];
  complete: boolean;
  choice: { decision_id: string; hypothesis_key: string } | null;
}

/**
 * §19 "Beberapa hipotesis bug -> Read-only investigations terpisah dengan timebox; integrator memilih perbaikan
 * berbukti": the per-hypothesis reports side by side (untrusted worker DATA; evidence references checked by the
 * runtime) and the integrator's choice of an EVIDENCED fix, recorded as a human APPROVAL for the plan's repair task.
 */
export function InvestigationPanel({ taskId, isAdmin }: { taskId: string; isAdmin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["investigation", taskId], queryFn: () => get<InvestigationView>(`/api/tasks/${encodeURIComponent(taskId)}/investigation`) });
  const [chosen, setChosen] = useState<string | null>(null);
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const v = q.data;
  return (
    <Card title={<span className="inline-flex items-center gap-2"><Microscope className="h-4 w-4" aria-hidden />Investigasi read-only per hipotesis</span>}>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : v ? (
        <div className="space-y-3 text-sm">
          <Notice tone="info">
            Setiap hipotesis diperiksa run terpisah, read-only (tanpa edit/shell/web), dengan timebox {v.timebox_seconds ?? "UNKNOWN"} detik. Laporan
            adalah data worker yang tidak tepercaya; referensi bukti hanya dicek keberadaannya oleh runtime. Perbaikan yang dipilih menjadi masukan
            task repair <Mono>{v.repair_task_key ?? "—"}</Mono>, yang tetap melalui admission, verifier, review dan acceptance.
          </Notice>
          <ul className="space-y-2">
            {v.hypotheses.map((h) => (
              <li key={h.key} className="rounded-md border border-line p-2 text-xs">
                <div className="flex flex-wrap items-center gap-1.5">
                  <Mono>{h.key}</Mono>
                  <span className="text-ink-2">{h.statement}</span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1.5">
                  <StatusBadge status={h.investigated ? "DONE" : "PENDING"} label={h.investigated ? "Sudah diperiksa" : "Belum diperiksa"} />
                  {h.verdict && <StatusBadge status={h.verdict === "SUPPORTED" ? "CONFIGURED" : "NOT_PROVEN"} label={`Verdict worker: ${h.verdict}`} />}
                  <StatusBadge status={h.evidenced ? "VERIFIED" : "NOT_PROVEN"} label={h.evidenced ? "Berbukti (referensi ada di repo)" : "Belum berbukti"} />
                  {h.outcome_code && <Mono>{h.outcome_code}</Mono>}
                </div>
                {h.summary && <p className="mt-1 text-ink-3">Ringkasan worker: {h.summary}</p>}
                {h.proposed_fix && <p className="mt-1 text-ink-3">Usulan perbaikan worker: {h.proposed_fix}</p>}
                {h.evidence_refs.length > 0 && (
                  <ul className="mt-1 list-disc pl-5">
                    {h.evidence_refs.map((r) => (
                      <li key={r.ref}>
                        <Mono>{r.ref}</Mono> {r.exists ? "(ada di repo)" : "(tidak ditemukan)"}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
          {v.choice ? (
            <StatusBadge status="APPROVED" label={`Integrator memilih ${v.choice.hypothesis_key} (keputusan ${v.choice.decision_id})`} />
          ) : v.complete && v.reason_code === "INVESTIGATION_COMPLETE" ? (
            v.evidenced.length === 0 ? (
              <Notice tone="warn">Tidak ada hipotesis berbukti: tidak ada perbaikan yang dapat dipilih (batalkan, atau beri attempt tambahan).</Notice>
            ) : (
              <div className="space-y-2 rounded-md border border-line bg-surface-2 p-3" role="group" aria-label="Pilih perbaikan berbukti">
                <fieldset className="flex flex-wrap gap-3">
                  <legend className="mb-1 text-sm font-medium">Perbaikan berbukti yang dipilih</legend>
                  {v.evidenced.map((k) => (
                    <label key={k} className="inline-flex items-center gap-1">
                      <input type="radio" name="investigation-choice" checked={chosen === k} onChange={() => setChosen(k)} /> {k}
                    </label>
                  ))}
                </fieldset>
                <Field label="Alasan (wajib, min 10 karakter)">
                  <textarea className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
                </Field>
                <ErrorBox error={error} />
                <Button
                  variant="primary"
                  busy={busy}
                  disabled={!isAdmin || !chosen || rationale.trim().length < 10}
                  title={isAdmin ? undefined : "Hanya SUPER_ADMIN; peran VIEWER hanya-baca"}
                  onClick={async () => {
                    setBusy(true);
                    setError(null);
                    try {
                      await post(`/api/tasks/${encodeURIComponent(taskId)}/investigation/choose`, { hypothesis_key: chosen, rationale: rationale.trim() });
                      qc.invalidateQueries({ queryKey: ["investigation", taskId] });
                    } catch (e) {
                      setError(e);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  Catat pilihan integrator
                </Button>
              </div>
            )
          ) : (
            <p className="text-xs text-ink-3">Pilihan integrator tersedia setelah semua hipotesis diperiksa ({v.state ?? "—"}/{v.reason_code || "—"}).</p>
          )}
        </div>
      ) : null}
    </Card>
  );
}
