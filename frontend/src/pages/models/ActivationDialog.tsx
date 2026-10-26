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
  const [recTraining, setRecTraining] = useState<"" | "yes" | "no">("");
  const [recTrial, setRecTrial] = useState<"" | "yes" | "no">("");
  // §44 DecisionRecord of an APPROVED adoption: options weighed, goal, cost/quality trade-off, invalidation condition
  const [options, setOptions] = useState("");
  const [goal, setGoal] = useState("");
  const [tradeoff, setTradeoff] = useState("");
  const [invalidateIf, setInvalidateIf] = useState("");
  const [clientAccepted, setClientAccepted] = useState(false);
  const [clientNote, setClientNote] = useState("");
  const activate = useActivateRoute(route.route_key);
  // a loopback LOCAL route spends no money through the broker (budget_required=false); older servers: effectively_free
  const paid = route.budget_required ?? !route.effectively_free;
  // ADR-0041 (2026-10-25): S07 "free-tier chat models require OpenCode" - the broker is its own client, so the route is
  // activated only with the owner's explicit decision, recorded on the decision and bound to the configuration hash
  const clientRestricted = route.client_restriction?.status === "OWNER_DECISION_REQUIRED";
  const clientDecided = !clientRestricted || (clientAccepted && clientNote.trim().length >= 10);
  const shapeRefused = route.upstream_request?.status === "REFUSED";
  // §23 "klaim provider bukan bukti": UNKNOWN training/trial terms limit the route to PUBLIC until the owner records
  // their own reading of the provider terms (terms_record, stored on the decision).
  const termsUnknown = (route.terms_unknown ?? []).length > 0;
  const flagRestricted =
    route.identity_status === "UNVERIFIED" ||
    route.data_terms?.may_use_inputs_for_training === true ||
    route.data_terms?.trial_only_no_confidential === true;
  const recordStarted = termsUnknown && (recTraining !== "" || recTrial !== "");
  const recordComplete = termsUnknown && termsReviewed && recTraining !== "" && recTrial !== "" && termsNote.trim().length >= 10;
  const recordClears = recordComplete && recTraining === "no" && recTrial === "no" && !flagRestricted;
  const restricted = (Boolean(route.terms_restriction) && !recordClears) || flagRestricted;
  const decisionKind = mode === "APPROVED" ? "ROUTE_ACTIVATION" : "TRIAL";
  const evalRunIds = evalRuns.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
  const optionList = options.split(/\n/).map((x) => x.trim()).filter(Boolean);
  const recordFilled = optionList.length > 0 && goal.trim().length > 0 && tradeoff.trim().length > 0 && invalidateIf.trim().length > 0;
  const canSubmit =
    rationale.trim().length >= 10 &&
    classes.length > 0 &&
    (!paid || budgetId.trim().length > 0) &&
    (mode !== "APPROVED" || (evalRunIds.length > 0 && recordFilled)) &&
    (!route.region_restricted || region.trim().length >= 2) &&
    (!recordStarted || recordComplete) &&
    (!restricted || classes.every((c) => c === "PUBLIC")) &&
    clientDecided &&
    !shapeRefused;

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
        terms_record: recordComplete
          ? { may_use_inputs_for_training: recTraining === "yes", trial_only_no_confidential: recTrial === "yes", note: termsNote.trim() }
          : null,
        eval_run_ids: mode === "APPROVED" ? evalRunIds : null,
        decision_record:
          mode === "APPROVED"
            ? { options: optionList, goal: goal.trim(), cost_quality_tradeoff: tradeoff.trim(), invalidate_if: invalidateIf.trim() }
            : null,
        client_terms: clientRestricted && clientAccepted ? { accepted: true, note: clientNote.trim() } : null,
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
        {shapeRefused && (
          <div className="mt-3">
            <Notice tone="warn">
              Broker tidak dapat membangun permintaan upstream untuk rute ini, jadi tidak ada keputusan yang dicatat:{" "}
              {route.upstream_request?.note}
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
              <fieldset className="rounded border border-line p-3">
                <legend className="px-1 text-sm font-medium">Catatan keputusan (DecisionRecord §44, wajib)</legend>
                <Field label="Opsi yang dipertimbangkan" hint="Satu opsi per baris, mis. tetap TRIAL / APPROVED / rute lain.">
                  <textarea className={inputClass} rows={2} value={options} onChange={(e) => setOptions(e.target.value)} />
                </Field>
                <Field label="Tujuan">
                  <input className={inputClass} value={goal} onChange={(e) => setGoal(e.target.value)} />
                </Field>
                <Field label="Trade-off biaya/kualitas">
                  <input className={inputClass} value={tradeoff} onChange={(e) => setTradeoff(e.target.value)} />
                </Field>
                <Field label="Batal berlaku bila" hint="Kondisi yang membuat keputusan ini tidak berlaku lagi.">
                  <input className={inputClass} value={invalidateIf} onChange={(e) => setInvalidateIf(e.target.value)} />
                </Field>
              </fieldset>
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
                  <input type="checkbox" checked={classes.includes(c)} disabled={restricted && c !== "PUBLIC" && !classes.includes(c)} onChange={() => toggle(c)} />
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
          {clientRestricted && (
            <fieldset className="rounded border border-line p-3">
              <legend className="px-1 text-sm font-medium">Syarat klien penyedia: keputusan Anda (wajib)</legend>
              <p className="mb-2 text-xs text-ink-3">
                Penyedia menyatakan: “{route.client_restriction?.statement ?? "UNKNOWN"}” ({route.client_restriction?.source ?? "sumber UNKNOWN"}).
                Broker AEOS memanggil upstream sebagai klien HTTP sendiri, bukan OpenCode; panggilan bisa ditolak dan itu baru terbukti lewat
                probe kelayakan. Memilih ini berarti Anda sendiri yang menilai dan menanggung kepatuhan pada syarat penyedia; AEOS
                tidak memilih untuk Anda. Pilihan lain: OpenCode Go, atau rute berbayar dengan keputusan BUDGET (ADR-0041).
              </p>
              <label className="flex items-start gap-2 text-sm">
                <input type="checkbox" checked={clientAccepted} onChange={(e) => setClientAccepted(e.target.checked)} />
                Saya memutuskan broker AEOS boleh memanggil rute ini sebagai klien sendiri. Keputusan ini tercatat pada keputusan dan
                menjadi bagian hash konfigurasi.
              </label>
              {clientAccepted && (
                <Field label="Catatan keputusan syarat klien" hint="Apa yang Anda baca/konfirmasi; minimal 10 karakter.">
                  <input aria-label="Catatan keputusan syarat klien" className={inputClass} value={clientNote} onChange={(e) => setClientNote(e.target.value)} />
                </Field>
              )}
              {!clientDecided && <p className="mt-1 text-xs text-warn">Tanpa keputusan ini rute tidak dapat diaktifkan.</p>}
            </fieldset>
          )}
          {route.client_restriction?.status === "OWNER_ACCEPTED" && (
            <Notice tone="info">
              Syarat klien penyedia sudah Anda putuskan pada konfigurasi ini
              {route.client_restriction.owner_note ? ` (catatan: ${route.client_restriction.owner_note})` : ""}; keputusan itu bagian dari hash
              yang diikat aktivasi ini. Apakah penyedia menerima panggilan broker tetap NOT-PROVEN sampai probe kelayakan.
            </Notice>
          )}
          {termsUnknown && (
            <fieldset className="rounded border border-line p-3">
              <legend className="px-1 text-sm font-medium">Terms belum diketahui: catat bacaan Anda</legend>
              <p className="mb-2 text-xs text-ink-3">
                Katalog tidak menyatakan {(route.terms_unknown ?? []).join(", ")}. Klaim provider bukan bukti; tanpa catatan Anda rute ini
                hanya untuk PUBLIC. Wajib: centang pemeriksaan terms di atas dan catatan minimal 10 karakter.
              </p>
              <Field label="Input dipakai untuk pelatihan model?">
                <select aria-label="Input dipakai untuk pelatihan model" className={inputClass} value={recTraining} onChange={(e) => setRecTraining(e.target.value as "" | "yes" | "no")}>
                  <option value="">Belum dicatat (UNKNOWN)</option>
                  <option value="yes">Ya - hanya PUBLIC</option>
                  <option value="no">Tidak</option>
                </select>
              </Field>
              <Field label="Hanya trial / tanpa data rahasia?">
                <select aria-label="Hanya trial tanpa data rahasia" className={inputClass} value={recTrial} onChange={(e) => setRecTrial(e.target.value as "" | "yes" | "no")}>
                  <option value="">Belum dicatat (UNKNOWN)</option>
                  <option value="yes">Ya - hanya PUBLIC</option>
                  <option value="no">Tidak</option>
                </select>
              </Field>
              {recordStarted && !recordComplete && (
                <p className="mt-1 text-xs text-warn">Lengkapi kedua jawaban, centang pemeriksaan terms dan isi catatan (min 10 karakter).</p>
              )}
            </fieldset>
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
