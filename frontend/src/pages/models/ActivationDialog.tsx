import { X } from "lucide-react";
import { useState } from "react";
import { Button, ErrorBox, Field, Mono, Notice, inputClass } from "../../components/ui";
import { shortHash } from "../../lib/format";
import { useActivateRoute } from "./api";
import { DATA_CLASSES, type RouteItem } from "./types";

/** Human activation (§20 AUTH): the dialog states the exact decision that will be recorded. */
export function ActivationDialog({ route, onClose }: { route: RouteItem; onClose: () => void }) {
  const [mode, setMode] = useState<"TRIAL" | "APPROVED">("TRIAL");
  const [rationale, setRationale] = useState("");
  const [classes, setClasses] = useState<string[]>(route.allowed_data_classes);
  const [budgetId, setBudgetId] = useState("");
  const [days, setDays] = useState<string>("14");
  const [termsReviewed, setTermsReviewed] = useState(false);
  const [termsNote, setTermsNote] = useState("");
  const [region, setRegion] = useState(route.region ?? "");
  const [evalRuns, setEvalRuns] = useState("");
  const activate = useActivateRoute(route.route_key);
  const paid = !route.effectively_free;
  const restricted =
    Boolean(route.terms_restriction) ||
    route.identity_status === "UNVERIFIED" ||
    route.data_terms?.may_use_inputs_for_training === true ||
    route.data_terms?.trial_only_no_confidential === true;
  const decisionKind = mode === "APPROVED" ? "ROUTE_ACTIVATION" : "TRIAL";
  const evalRunIds = evalRuns.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
  const canSubmit =
    rationale.trim().length >= 10 &&
    classes.length > 0 &&
    (!paid || budgetId.trim().length > 0) &&
    (mode !== "APPROVED" || evalRunIds.length > 0) &&
    (!route.region_restricted || region.trim().length >= 2);

  const toggle = (c: string) => setClasses((cur) => (cur.includes(c) ? cur.filter((x) => x !== c) : [...cur, c]));

  const submit = () =>
    activate.mutate(
      {
        mode,
        rationale: rationale.trim(),
        expected_config_hash: route.config_hash,
        allowed_data_classes: classes,
        budget_decision_id: paid ? budgetId.trim() : null,
        expires_in_days: days ? Number(days) : null,
        terms_reviewed: termsReviewed,
        terms_note: termsReviewed ? termsNote.trim() : "",
        region: region.trim() ? region.trim().toUpperCase() : null,
        eval_run_ids: mode === "APPROVED" ? evalRunIds : null,
      },
      { onSuccess: onClose },
    );

  return (
    <div role="dialog" aria-modal="true" aria-label="Aktivasi rute" className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border border-line bg-surface p-5 shadow-xl">
        <div className="mb-3 flex items-start justify-between gap-2">
          <h2 className="text-base font-semibold">Aktivasi rute {route.display_name || route.model_id}</h2>
          <button type="button" onClick={onClose} aria-label="Tutup" className="rounded p-1 hover:bg-surface-3">
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
        <Notice tone="info">
          Keputusan yang akan dicatat: <b>{decisionKind}</b> oleh Anda (manusia), terikat ke hash konfigurasi{" "}
          <Mono>{shortHash(route.config_hash, 16)}</Mono>, domain <b>PERSONAL</b>, operasi <Mono>route.use</Mono>. Bila
          konfigurasi rute berubah (alias, versi, harga, data terms), keputusan ini otomatis tidak berlaku lagi. OFFICE tidak
          dapat diaktifkan di sini.
        </Notice>
        {paid && (
          <div className="mt-3">
            <Notice tone="warn">
              Rute ini berbayar atau status gratisnya tidak terjamin. Aktivasi memerlukan ID keputusan <b>BUDGET</b> yang sah dan
              terpisah; tidak ada fallback berbayar otomatis.
            </Notice>
          </div>
        )}
        <div className="mt-4 space-y-3">
          <Field label="Jenis keputusan">
            <select className={inputClass} value={mode} onChange={(e) => setMode(e.target.value as "TRIAL" | "APPROVED")}>
              <option value="TRIAL">TRIAL - uji terbatas, kedaluwarsa</option>
              <option value="APPROVED">APPROVED - rute produksi pekerja</option>
            </select>
          </Field>
          {mode === "APPROVED" && (
            <>
              <Notice tone="warn">
                Rute produksi hanya untuk konfigurasi yang diizinkan <b>dan telah diuji</b> (§23): sebutkan EvalRun COMPLETED yang lulus
                gate pada konfigurasi persis ini. Tanpa itu gunakan TRIAL.
              </Notice>
              <Field label="ID EvalRun bukti (wajib untuk APPROVED)" hint="Pisahkan dengan koma; run konfigurasi lain atau yang gagal gate ditolak backend.">
                <input className={inputClass} value={evalRuns} onChange={(e) => setEvalRuns(e.target.value)} placeholder="uuid EvalRun" />
              </Field>
            </>
          )}
          <Field label="Alasan (wajib)" hint="Minimal 10 karakter; tersimpan pada keputusan dan audit.">
            <textarea className={inputClass} rows={3} value={rationale} onChange={(e) => setRationale(e.target.value)} />
          </Field>
          <fieldset>
            <legend className="mb-1 text-sm font-medium">Kelas data yang diizinkan</legend>
            <div className="flex flex-wrap gap-3 text-sm">
              {DATA_CLASSES.map((c) => (
                <label key={c} className="inline-flex items-center gap-1">
                  <input type="checkbox" checked={classes.includes(c)} disabled={restricted && c !== "PUBLIC"} onChange={() => toggle(c)} />
                  {c}
                </label>
              ))}
            </div>
            {restricted && (
              <p className="mt-1 text-xs text-warn">
                Data terms/identitas rute ini hanya mengizinkan PUBLIC{route.terms_restriction ? ` (${route.terms_restriction})` : ""}.
              </p>
            )}
          </fieldset>
          {route.region_restricted && (
            <Field label="Region / residensi yang Anda periksa (wajib)" hint="Provider membatasi region; contoh ID, EU, GLOBAL.">
              <input className={inputClass} value={region} onChange={(e) => setRegion(e.target.value)} placeholder="ID" />
            </Field>
          )}
          {paid && (
            <Field label="ID keputusan BUDGET">
              <input className={inputClass} value={budgetId} onChange={(e) => setBudgetId(e.target.value)} placeholder="uuid keputusan BUDGET" />
            </Field>
          )}
          <Field label="Kedaluwarsa (hari)" hint={mode === "TRIAL" ? "TRIAL 1-90 hari" : "Opsional, 1-365 hari"}>
            <input className={inputClass} inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value.replace(/[^0-9]/g, ""))} />
          </Field>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" checked={termsReviewed} onChange={(e) => setTermsReviewed(e.target.checked)} />
            Saya sudah membaca ulang harga dan data terms rute ini sekarang (memperbarui waktu pemeriksaan). Wajib bila belum pernah
            diperiksa pemilik atau lebih dari 30 hari; tanggal riset katalog bukan pemeriksaan terms.
          </label>
          {termsReviewed && (
            <Field label="Catatan pemeriksaan terms" hint="Apa yang dibaca (retensi, pelatihan, trial); tersimpan pada keputusan.">
              <input className={inputClass} value={termsNote} onChange={(e) => setTermsNote(e.target.value)} />
            </Field>
          )}
        </div>
        {activate.error && (
          <div className="mt-3">
            <ErrorBox error={activate.error} />
          </div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <Button onClick={onClose}>Batal</Button>
          <Button variant="primary" disabled={!canSubmit} busy={activate.isPending} onClick={submit}>
            Catat keputusan {decisionKind}
          </Button>
        </div>
      </div>
    </div>
  );
}
