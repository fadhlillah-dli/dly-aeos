import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { post } from "../../api/client";
import type { Domain } from "../../auth/AuthContext";
import { Button, Card, ErrorBox, Field, inputClass, Notice } from "../../components/ui";

export type DecisionKind = "EXCEPTION" | "RISK_ACCEPTANCE" | "WRITE_ENABLEMENT" | "POLICY" | "BUDGET";

/** One provider vocabulary (backend aeos.portfolio.providers): Google Docs live in the google-drive target. */
export const PROVIDERS: Record<Domain, string[]> = {
  PERSONAL: ["google-drive", "github-issues", "github-repo"],
  OFFICE: ["confluence", "jira", "bitbucket"],
};

/** Exception presets with the exact target shape their consumer reads (PERSONAL only). */
export const EXCEPTION_PRESETS: Record<string, { label: string; target: string; consumer: string; risk: string }> = {
  secret_store_interim_keyring: {
    label: "Secret store sementara di OS keyring (OpenBao belum tersedia)",
    target: "secret_store/interim-keyring",
    consumer: "aeos.secrets.provider",
    risk: "Tanpa TTL/lease, audit vault dan ACL per namespace; proses dengan user OS yang sama dapat membaca nilai. OFFICE tidak pernah diterima.",
  },
  worker_sandbox_process: {
    label: "Worker di process sandbox (WEAK) untuk PERSONAL",
    target: "worker_sandbox/process",
    consumer: "aeos.worker_runtime.sandbox",
    risk: "Tidak ada penegakan egress/filesystem oleh OS; user OS sama. Shell (bash) dan egress tidak pernah diberikan di process sandbox (master §25, amendemen ADR-0051 2026-10-19); tugas R3 hanya bila operasi run_worker_process_sandbox_r3 dicantumkan, R4 selalu BLOCKED.",
  },
};

const OFFICE_KEYS = ["external_worker_allowed", "approved_worker_routes", "claude_max_allowed_for_office_data", "trace_destinations_allowed", "requires_isolated_runtime", "atlassian_deployment"];
/** Data classes the company can approve for AI processing of OFFICE data (backend DataClassification). */
export const DATA_CLASSES = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"];

/** One "office/<provider>=<identity>" per line -> {ref: identity}; the backend validates refs and values. */
export function parseIdentities(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split(/\n/)) {
    const t = line.trim();
    if (!t) continue;
    const i = t.indexOf("=");
    if (i <= 0) throw new Error(`identitas "${t}" harus berbentuk office/<provider>=<identitas>`);
    out[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  }
  return out;
}

/** One approved target per line ("provider:kunci=nilai,kunci=nilai"); parsed and validated by the backend. */
export function parseTargets(text: string): string[] {
  return text
    .split(/\n/)
    .map((l) => l.trim())
    .filter(Boolean);
}

/** "k=v; k2=v2" -> {k: "v", k2: "v2"}; JSON objects are accepted as-is. */
export function parseScope(text: string): Record<string, unknown> {
  const t = text.trim();
  if (!t) return {};
  if (t.startsWith("{")) {
    const v = JSON.parse(t) as unknown;
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
  }
  const out: Record<string, unknown> = {};
  for (const part of t.split(/[;\n]/)) {
    const [k, ...rest] = part.split("=");
    if (k?.trim() && rest.length) out[k.trim()] = rest.join("=").trim();
  }
  return out;
}

export function DecisionForm({ domain, requestId, initialKind, onDone }: { domain: Domain; requestId?: string | null; initialKind?: DecisionKind; onDone: () => void }) {
  const qc = useQueryClient();
  const [kind, setKind] = useState<DecisionKind>(initialKind ?? "EXCEPTION");
  const [targetType, setTargetType] = useState("");
  const [targetId, setTargetId] = useState("");
  const [targetHash, setTargetHash] = useState("");
  const [ops, setOps] = useState("");
  const [scope, setScope] = useState("");
  const [rationale, setRationale] = useState("");
  const [expires, setExpires] = useState("");
  const [provider, setProvider] = useState(PROVIDERS[domain][0]);
  const [enabled, setEnabled] = useState(true);
  const [officeAuth, setOfficeAuth] = useState(false);
  const [companyRef, setCompanyRef] = useState("");
  const [allowedProviders, setAllowedProviders] = useState<string[]>([]);
  const [officeOverrides, setOfficeOverrides] = useState("");
  // §01 U-03 / §07 step 10 / §51 P1: what the company approved; empty = UNDECIDED (consumers fail closed)
  const [approvedTargets, setApprovedTargets] = useState("");
  const [approvedClasses, setApprovedClasses] = useState<string[]>([]);
  const [approvedIdentities, setApprovedIdentities] = useState("");
  const [preset, setPreset] = useState<string>("");
  const [allowBash, setAllowBash] = useState(false);
  // §41/§43 system change: a POLICY on a policy/model/tool/hook/prompt/memory/fallback/context target needs
  // COMPLETED all-gates-PASS evaluation runs and the rollback target it replaces (backend SYSTEM_CHANGE_TARGET_TYPES)
  const [scEvalRuns, setScEvalRuns] = useState("");
  const [scRollback, setScRollback] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const usePreset = kind === "EXCEPTION" && domain === "PERSONAL" && !!preset;

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => post("/api/decisions", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["decisions"] });
      qc.invalidateQueries({ queryKey: ["decision-queue"] });
      qc.invalidateQueries({ queryKey: ["policy"] });
      onDone();
    },
  });

  const submit = () => {
    setLocalError(null);
    let authScope: Record<string, unknown>;
    let overrides: Record<string, unknown> | null = null;
    let identities: Record<string, string> = {};
    try {
      authScope = parseScope(scope);
      if (officeOverrides.trim()) overrides = JSON.parse(officeOverrides) as Record<string, unknown>;
      if (officeAuth) identities = parseIdentities(approvedIdentities);
    } catch (e) {
      setLocalError(`Format tidak valid: ${e instanceof Error ? e.message : String(e)}`);
      return;
    }
    if (!Object.keys(authScope).length) {
      setLocalError("Scope eksplisit wajib diisi (mis. repo=owner/nama; tujuan=...).");
      return;
    }
    const body: Record<string, unknown> = {
      kind,
      rationale,
      auth_scope: authScope,
      allowed_operations: ops.split(/[\s,]+/).filter(Boolean),
      request_id: requestId ?? undefined,
    };
    if (expires) body.expires_at = new Date(expires).toISOString();
    if (kind === "WRITE_ENABLEMENT") {
      body.provider = provider;
      body.enabled = enabled;
    } else if (usePreset) {
      body.exception = preset;
      if (preset === "worker_sandbox_process") body.allow_bash = allowBash;
    } else if (kind === "POLICY" && domain === "OFFICE" && (officeAuth || overrides)) {
      body.office_authorization = officeAuth;
      if (officeAuth) {
        body.company_reference = companyRef;
        body.allowed_providers = allowedProviders;
        body.approved_targets = parseTargets(approvedTargets);
        body.approved_data_classes = approvedClasses;
        body.approved_identities = identities;
      }
      if (overrides) body.office_policy = overrides;
    } else {
      body.target_type = targetType;
      body.target_id = targetId;
      if (targetHash) body.target_hash = targetHash;
      const evalRunIds = scEvalRuns.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
      if (kind === "POLICY" && (evalRunIds.length || scRollback.trim())) {
        body.details = { system_change: { eval_run_ids: evalRunIds, rollback_target: scRollback.trim() } };
      }
    }
    save.mutate(body);
  };

  const needsTarget = kind !== "WRITE_ENABLEMENT" && !usePreset && !(kind === "POLICY" && domain === "OFFICE" && officeAuth);
  return (
    <Card title="Catat keputusan manusia">
      <div className="grid gap-3">
        <Notice tone="info">
          Keputusan ini adalah AUTH Anda sebagai pemilik, terikat target dan scope eksplisit, dan diaudit. Persetujuan rencana dilakukan di halaman rencana
          (terikat versi + hash). Domain: <strong>{domain}</strong>.
        </Notice>
        <Field label="Jenis keputusan">
          <select className={inputClass} value={kind} onChange={(e) => setKind(e.target.value as DecisionKind)}>
            <option value="EXCEPTION">EXCEPTION — pengecualian berbatas waktu</option>
            <option value="RISK_ACCEPTANCE">RISK_ACCEPTANCE — penerimaan risiko residual</option>
            <option value="WRITE_ENABLEMENT">WRITE_ENABLEMENT — izinkan/hentikan tulis ke provider</option>
            <option value="POLICY">POLICY — kebijakan (termasuk otorisasi perusahaan OFFICE)</option>
            <option value="BUDGET">BUDGET — keputusan anggaran (diproses modul biaya)</option>
          </select>
        </Field>
        {kind === "EXCEPTION" &&
          (domain === "PERSONAL" ? (
            <div className="grid gap-3 rounded-md border border-line p-3">
              <Field label="Jenis pengecualian" hint="Preset memakai target persis yang dibaca modul pemakainya.">
                <select aria-label="Jenis pengecualian" className={inputClass} value={preset} onChange={(e) => setPreset(e.target.value)}>
                  <option value="">Lainnya (target manual)</option>
                  {Object.entries(EXCEPTION_PRESETS).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v.label}
                    </option>
                  ))}
                </select>
              </Field>
              {usePreset && (
                <Notice tone="warn">
                  Target <strong>{EXCEPTION_PRESETS[preset].target}</strong> (dibaca oleh {EXCEPTION_PRESETS[preset].consumer}). Risiko residual:{" "}
                  {EXCEPTION_PRESETS[preset].risk} Wajib berlaku sampai tanggal tertentu.
                </Notice>
              )}
              {preset === "worker_sandbox_process" && (
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={allowBash} onChange={(e) => setAllowBash(e.target.checked)} /> Izinkan bash (allow_bash) — hanya
                  dicatat: tidak membuka shell di process sandbox (master §25); shell hanya di sandbox container yang containment-nya lulus
                </label>
              )}
            </div>
          ) : (
            <Notice tone="info">Pengecualian secret store sementara dan process sandbox hanya untuk PERSONAL; OFFICE selalu ditolak.</Notice>
          ))}
        {kind === "WRITE_ENABLEMENT" && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Provider">
              <select className={inputClass} value={provider} onChange={(e) => setProvider(e.target.value)}>
                {PROVIDERS[domain].map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
            </Field>
            <Field label="Status tulis">
              <select className={inputClass} value={enabled ? "on" : "off"} onChange={(e) => setEnabled(e.target.value === "on")}>
                <option value="on">Izinkan tulis (tetap butuh approval versi + interlock)</option>
                <option value="off">Hentikan tulis</option>
              </select>
            </Field>
          </div>
        )}
        {kind === "POLICY" && domain === "OFFICE" && (
          <div className="grid gap-3 rounded-md border border-line p-3">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={officeAuth} onChange={(e) => setOfficeAuth(e.target.checked)} /> Ini otorisasi perusahaan untuk OFFICE
            </label>
            {officeAuth && (
              <>
                <Field label="Referensi persetujuan perusahaan" hint="Nomor/tautan dokumen persetujuan resmi (bukan isi rahasia).">
                  <input className={inputClass} value={companyRef} onChange={(e) => setCompanyRef(e.target.value)} />
                </Field>
                <fieldset className="flex flex-wrap gap-3 text-sm">
                  <legend className="mb-1 text-sm font-medium">Provider yang diizinkan</legend>
                  {PROVIDERS.OFFICE.map((p) => (
                    <label key={p} className="flex items-center gap-1">
                      <input
                        type="checkbox"
                        checked={allowedProviders.includes(p)}
                        onChange={(e) => setAllowedProviders(e.target.checked ? [...allowedProviders, p] : allowedProviders.filter((x) => x !== p))}
                      />
                      {p}
                    </label>
                  ))}
                </fieldset>
                <Field
                  label="Target yang disetujui perusahaan (satu per baris)"
                  hint="provider:kunci=nilai,kunci=nilai. Confluence/Jira wajib tenant (confluence:tenant=perusahaan.atlassian.net,space_id=98765 · jira:tenant=perusahaan.atlassian.net,root_id=ENG); Bitbucket wajib workspace (bitbucket:workspace=perusahaan[,root_id=perusahaan/repo]). Kosong = belum diputuskan: semua target OFFICE ditolak."
                >
                  <textarea
                    aria-label="Target yang disetujui perusahaan"
                    className={`${inputClass} mono text-xs`}
                    value={approvedTargets}
                    onChange={(e) => setApprovedTargets(e.target.value)}
                    placeholder={"confluence:tenant=perusahaan.atlassian.net,space_id=98765\nbitbucket:workspace=perusahaan"}
                  />
                </Field>
                <fieldset className="flex flex-wrap gap-3 text-sm">
                  <legend className="mb-1 text-sm font-medium">Kelas data yang disetujui untuk AI (route worker / Claude Max)</legend>
                  {DATA_CLASSES.map((c) => (
                    <label key={c} className="flex items-center gap-1">
                      <input
                        type="checkbox"
                        checked={approvedClasses.includes(c)}
                        onChange={(e) => setApprovedClasses(e.target.checked ? [...approvedClasses, c] : approvedClasses.filter((x) => x !== c))}
                      />
                      {c}
                    </label>
                  ))}
                  <span className="w-full text-xs text-ink-3">Tidak ada yang dicentang = belum diputuskan: data OFFICE tidak dikirim ke model mana pun.</span>
                </fieldset>
                <Field
                  label="Identitas credential yang disetujui (opsional, satu per baris)"
                  hint="office/atlassian=<account id atau email>; office/bitbucket=<uuid atau username>. Dibandingkan Doctor (secret.office_identity) dengan akun credential tersimpan — bukan secret."
                >
                  <textarea
                    aria-label="Identitas credential yang disetujui"
                    className={`${inputClass} mono text-xs`}
                    value={approvedIdentities}
                    onChange={(e) => setApprovedIdentities(e.target.value)}
                    placeholder="office/atlassian=akun@perusahaan.example"
                  />
                </Field>
              </>
            )}
            <Field label="Override kebijakan OFFICE (JSON, opsional)" hint={`Hanya kunci: ${OFFICE_KEYS.join(", ")}. Berlaku hanya saat otorisasi perusahaan aktif.`}>
              <textarea className={`${inputClass} mono text-xs`} value={officeOverrides} onChange={(e) => setOfficeOverrides(e.target.value)} placeholder='{"atlassian_deployment": "CLOUD"}' />
            </Field>
          </div>
        )}
        {needsTarget && (
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Jenis target">
              <input className={inputClass} value={targetType} onChange={(e) => setTargetType(e.target.value)} placeholder="repository / route / budget / risk" />
            </Field>
            <Field label="ID target persis">
              <input className={inputClass} value={targetId} onChange={(e) => setTargetId(e.target.value)} />
            </Field>
            <Field label="Hash target (opsional)">
              <input className={inputClass} value={targetHash} onChange={(e) => setTargetHash(e.target.value)} placeholder="sha256:…" />
            </Field>
          </div>
        )}
        {needsTarget && kind === "POLICY" && (
          <fieldset className="grid gap-3 rounded-md border border-line p-3 sm:grid-cols-2">
            <legend className="px-1 text-sm font-medium">System change (§41/§43)</legend>
            <p className="text-xs text-ink-3 sm:col-span-2">
              Wajib bila target adalah policy, model/route, pin model tahap (planning_baseline), tool schema/tools, hooks, prompt/instruction, schema, skill/agent definition, memory, fallback, context strategy, retrieval atau harness:
              EvalRun COMPLETED dengan semua gate PASS dan target rollback. Kebijakan tata kelola lain tercatat sebagai bukan system change.
            </p>
            <Field label="ID EvalRun bukti" hint="Pisahkan dengan koma.">
              <input className={inputClass} value={scEvalRuns} onChange={(e) => setScEvalRuns(e.target.value)} placeholder="uuid EvalRun" />
            </Field>
            <Field label="Target rollback" hint="Versi/konfigurasi/keputusan yang digantikan.">
              <input className={inputClass} value={scRollback} onChange={(e) => setScRollback(e.target.value)} placeholder="mis. prompt rev r41" />
            </Field>
          </fieldset>
        )}
        {kind !== "WRITE_ENABLEMENT" && (
          <Field label="Operasi yang diizinkan (pisahkan koma)" hint="Merge/produksi adalah tindakan manusia dan akan ditolak.">
            <input className={inputClass} value={ops} onChange={(e) => setOps(e.target.value)} />
          </Field>
        )}
        <Field label="Scope eksplisit (wajib)" hint="key=value; key2=value2 atau JSON. Contoh: repo=owner/nama; alasan=hotfix">
          <textarea className={inputClass} value={scope} onChange={(e) => setScope(e.target.value)} />
        </Field>
        <Field label={kind === "EXCEPTION" || kind === "RISK_ACCEPTANCE" ? "Berlaku sampai (wajib)" : "Berlaku sampai (opsional)"}>
          <input type="datetime-local" className={inputClass} value={expires} onChange={(e) => setExpires(e.target.value)} />
        </Field>
        <Field label="Rationale (wajib, min. 10 karakter)">
          <textarea className={inputClass} value={rationale} onChange={(e) => setRationale(e.target.value)} />
        </Field>
        {localError && <div className="text-sm text-bad">{localError}</div>}
        <ErrorBox error={save.error} />
        <div className="flex gap-2">
          <Button variant="primary" busy={save.isPending} disabled={rationale.trim().length < 10} onClick={submit}>
            Catat keputusan
          </Button>
          <Button onClick={onDone}>Batal</Button>
        </div>
      </div>
    </Card>
  );
}
