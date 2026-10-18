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
    risk: "Tidak ada penegakan egress/filesystem oleh OS; user OS sama. bash hanya bila allow_bash dicentang (ADR-0051).",
  },
};

const OFFICE_KEYS = ["external_worker_allowed", "approved_worker_routes", "claude_max_allowed_for_office_data", "trace_destinations_allowed", "requires_isolated_runtime", "atlassian_deployment"];

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
  const [preset, setPreset] = useState<string>("");
  const [allowBash, setAllowBash] = useState(false);
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
    try {
      authScope = parseScope(scope);
      if (officeOverrides.trim()) overrides = JSON.parse(officeOverrides) as Record<string, unknown>;
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
      }
      if (overrides) body.office_policy = overrides;
    } else {
      body.target_type = targetType;
      body.target_id = targetId;
      if (targetHash) body.target_hash = targetHash;
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
                  <input type="checkbox" checked={allowBash} onChange={(e) => setAllowBash(e.target.checked)} /> Izinkan bash (allow_bash) — worker
                  menjalankan perintah sebagai user OS yang sama
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
