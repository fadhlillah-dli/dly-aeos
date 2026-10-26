import { Gauge } from "lucide-react";
import { useState } from "react";
import { post } from "../../api/client";
import { StatusBadge } from "../StatusBadge";
import { Button, ErrorBox, Field, inputClass, Mono, Notice } from "../ui";

/** GET /api/workforce/roles -> role_eval_threshold (backend workforce.scheduler.role_eval_threshold). */
export interface RoleEvalThresholdView {
  required_from: string;
  default: string;
  source: "DEFAULT" | "OWNER_SETTING" | string;
  risk_classes: string[];
  requires_evaluation: string[];
  setting?: string;
}

const RISKS = ["R0", "R1", "R2", "R3", "R4"];

function from(risks: string[], value: string): string[] {
  const i = risks.indexOf(value);
  return i < 0 ? [] : risks.slice(i);
}

/**
 * §46 "agent menjadi eligible untuk role dari hasil evaluasi": from which risk class admission needs a PASS role
 * evaluation that the owner adopted (or an owner EXCEPTION). R4 is the pilot default; a lower class tightens admission.
 * Owner action with an explicit review step before it is sent (POST /api/workforce/role-eval-threshold, audited).
 */
export function RoleEvalThreshold({ threshold, isAdmin, onChanged }: { threshold: RoleEvalThresholdView; isAdmin: boolean; onChanged: () => void }) {
  const risks = threshold.risk_classes?.length ? threshold.risk_classes : RISKS;
  const [open, setOpen] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [value, setValue] = useState(threshold.required_from);
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<{ previous: string; threshold: string } | null>(null);
  const valid = risks.includes(value) && value !== threshold.required_from && rationale.trim().length >= 5;
  const close = () => {
    setOpen(false);
    setReviewing(false);
    setError(null);
  };
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await post<{ previous: string; threshold: string }>("/api/workforce/role-eval-threshold", { risk_class: value, rationale: rationale.trim() });
      setDone(out);
      close();
      setRationale("");
      onChanged();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-2 text-sm" data-testid="role-eval-threshold">
      <div className="flex flex-wrap items-center gap-2">
        <span>
          Evaluasi peran wajib mulai risiko <Mono>{threshold.required_from}</Mono>: task{" "}
          {threshold.requires_evaluation.join(", ") || "—"} hanya diterima admission dengan hasil evaluasi PASS yang diadopsi pemilik atau EXCEPTION pemilik.
        </span>
        {threshold.source === "OWNER_SETTING" ? (
          <StatusBadge status="CONFIGURED" label="Ditetapkan pemilik" />
        ) : (
          <StatusBadge status="NOT_CHECKED" label={`Bawaan pilot (${threshold.default})`} />
        )}
      </div>
      <div aria-live="polite">
        {done && (
          <Notice tone="info">
            Ambang tercatat: {done.previous} → {done.threshold} (audit <Mono>workforce.role_eval_threshold</Mono>). Berlaku untuk admission berikutnya.
          </Notice>
        )}
      </div>
      {!open && (
        <Button
          variant="secondary"
          disabled={!isAdmin}
          title={isAdmin ? undefined : "Hanya SUPER_ADMIN; peran VIEWER hanya-baca"}
          onClick={() => {
            setValue(threshold.required_from);
            setOpen(true);
          }}
        >
          <Gauge className="h-4 w-4" aria-hidden /> Ubah ambang evaluasi peran…
        </Button>
      )}
      {open && isAdmin && (
        <div role="dialog" aria-label="Ambang evaluasi peran" className="space-y-2 rounded-md border border-line bg-surface-2 p-3">
          {!reviewing ? (
            <>
              <Field label="Evaluasi peran wajib mulai risiko" hint="Kelas lebih rendah memperketat admission: R2 berarti task R2, R3 dan R4 memerlukan peran yang dievaluasi.">
                <select aria-label="Evaluasi peran wajib mulai risiko" className={inputClass} value={value} onChange={(e) => setValue(e.target.value)}>
                  {risks.map((r) => (
                    <option key={r} value={r}>
                      {r}
                      {r === threshold.default ? " (bawaan pilot)" : ""}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Alasan (wajib, min. 5 karakter)" hint="Tersimpan di audit.">
                <textarea aria-label="Alasan ambang evaluasi peran" className={inputClass} rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
              </Field>
              {value === threshold.required_from && <p className="text-xs text-ink-3">Pilih kelas risiko yang berbeda dari ambang sekarang.</p>}
              <div className="flex flex-wrap gap-2">
                <Button variant="primary" disabled={!valid} onClick={() => setReviewing(true)}>
                  Tinjau perubahan
                </Button>
                <Button variant="ghost" onClick={close}>
                  Batal
                </Button>
              </div>
            </>
          ) : (
            <>
              <Notice tone="warn">
                Yang akan dicatat oleh Anda (manusia): ambang <Mono>{threshold.required_from}</Mono> → <Mono>{value}</Mono>. Task risiko{" "}
                <strong>{from(risks, value).join(", ")}</strong> akan memerlukan hasil evaluasi PASS yang diadopsi atau EXCEPTION pemilik
                {RISKS.indexOf(value) > RISKS.indexOf(threshold.required_from) ? "; ini MELONGGARKAN admission dibanding sekarang" : ""}. Perubahan diaudit dan
                dapat diubah lagi kapan saja.
              </Notice>
              <ErrorBox error={error} />
              <div className="flex flex-wrap gap-2">
                <Button variant="primary" busy={busy} onClick={submit}>
                  Konfirmasi ambang {value}
                </Button>
                <Button variant="ghost" onClick={() => setReviewing(false)}>
                  Kembali
                </Button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
