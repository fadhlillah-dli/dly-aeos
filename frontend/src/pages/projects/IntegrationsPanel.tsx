import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Terminal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { get, put } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, Freshness, inputClass, Loading, Mono, Notice } from "../../components/ui";
import { absoluteTime } from "../../lib/format";
import { GATE_HELP } from "../evidence/labels";
import { ModuleBadge } from "./badges";
import { DeploymentTargetsPanel } from "./DeploymentTargetsPanel";
import { targetPathOk } from "./deliveryTypes";
import type { IntegrationView, ListResponse, ProjectView } from "./types";

const PURPOSE_LABEL: Record<string, string> = {
  docs: "Dokumentasi",
  issues: "Tracking kerja",
  code: "Kode",
  ci: "CI",
  roadmap: "Roadmap",
  staging: "Staging",
  production: "Produksi",
  deploy: "Deploy",
};
/** One provider vocabulary (backend aeos.portfolio.providers). */
export const PROVIDER_LABEL: Record<string, string> = {
  "google-drive": "Google Drive / Google Docs",
  "github-issues": "GitHub Issues",
  "github-repo": "GitHub repository",
  "github-projects": "GitHub Projects",
  "github-actions": "GitHub Actions",
  confluence: "Confluence",
  jira: "Jira",
  bitbucket: "Bitbucket",
  "bitbucket-pipelines": "Bitbucket Pipelines",
  "deployment-target": "Deployment target",
};
const ROOT_HINT: Record<string, string> = {
  "google-drive": "ID folder Google Drive (GOOGLE_DRIVE_FOLDER_ID), opsional",
  confluence: "CONFLUENCE_PROJECTS_PARENT_ID (angka)",
  jira: "Kunci proyek Jira (JIRA_PROJECT_KEY), mis. AEOS",
  "github-issues": "owner/nama repository (GITHUB_REPOSITORY)",
  "github-repo": "owner/nama repository",
  bitbucket: "workspace/repo_slug (BITBUCKET_REPOSITORY)",
  "deployment-target": "URL dasar https target (DEPLOYMENT_TARGET_URL), mis. https://staging.contoh.id",
};
/** Gates a project's pipeline reference maps (backend delivery.provider.PIPELINE_GATES). */
export const PIPELINE_GATES = ["G1", "G2", "G3", "G5"] as const;
/** Same rule as the backend deployment observer (delivery.deployment.health_path_ok): no '//' host, no '..'. */
export const HEALTH_PATH = /^\/(?!\/)(?!(?:.*\/)?\.\.(?:\/|$))[A-Za-z0-9._~/-]{0,200}$/;
/** A PERSONAL secret-store reference (personal/... or projects/<project>/...), never a value; the backend re-validates. */
export const PERSONAL_CREDENTIAL_REF = /^(?!.*\.\.)(?:personal\/[A-Za-z0-9._-][A-Za-z0-9._/-]{0,190}|projects\/[A-Za-z0-9._-]+\/[A-Za-z0-9._/-]{1,190})$/;
/** deployment-target config keys the Integration Registry keeps when a PUT omits them (an explicit "" clears). */
const KEPT_WHEN_OMITTED = new Set(["health_path", "smoke_checks", "sli_checks", "rollback_on", "require_artifact_digest"]);

interface IntegrationList extends ListResponse<IntegrationView> {
  allowed_providers: Record<string, string[]>;
}

function IntegrationRow({ i }: { i: IntegrationView }) {
  const cap = i.capability_status ?? {};
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{PROVIDER_LABEL[i.provider] ?? i.provider}</span>
        <Mono>{i.provider}</Mono>
        <span className="text-xs text-ink-3">{PURPOSE_LABEL[i.purpose] ?? i.purpose}</span>
        <ModuleBadge kind="config" value={i.last_verified_at ? "VERIFIED" : cap.configuration} />
        <ModuleBadge kind="credential" value={cap.credential?.status} />
        {/* Labels follow the actual status code; only NOT_CHECKED/NOT_TESTED get the "belum" wording. */}
        <StatusBadge
          status={cap.connectivity ?? "NOT_CHECKED"}
          label={(cap.connectivity ?? "NOT_CHECKED") === "NOT_CHECKED" ? "Koneksi belum diperiksa" : `Koneksi: ${cap.connectivity}`}
        />
        <StatusBadge
          status={cap.functional ?? "NOT_TESTED"}
          label={(cap.functional ?? "NOT_TESTED") === "NOT_TESTED" ? "Fungsi belum diuji" : `Fungsi: ${cap.functional}`}
        />
        {cap.office_default_deny && <StatusBadge status="BLOCKED" label="OFFICE: default deny" />}
        {cap.atlassian_deployment && (
          <StatusBadge
            status={cap.atlassian_deployment === "UNKNOWN" ? "UNKNOWN" : "CONFIGURED"}
            label={cap.atlassian_deployment === "UNKNOWN" ? "Cloud/Data Center belum diverifikasi" : `Dideklarasikan ${cap.atlassian_deployment}`}
          />
        )}
        {i.target_url && (
          <a href={i.target_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
            Buka target <ExternalLink className="h-3 w-3" aria-hidden />
          </a>
        )}
      </div>
      <dl className="mt-2 grid grid-cols-[minmax(120px,max-content)_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-ink-3">{i.root_id_name ?? "Root ID"}</dt>
        <dd>
          {i.root_id ? (
            <Mono>{i.root_id}</Mono>
          ) : cap.root === "DEFAULT" ? (
            <span>My Drive (folder proyek dibuat oleh aplikasi)</span>
          ) : (
            <span className="text-warn">belum diatur</span>
          )}
        </dd>
        {cap.target && (
          <>
            <dt className="text-ink-3">Target persetujuan</dt>
            <dd>
              <Mono>{JSON.stringify(cap.target)}</Mono>
            </dd>
          </>
        )}
        {i.tenant_id && (
          <>
            <dt className="text-ink-3">Tenant</dt>
            <dd>
              <Mono>{i.tenant_id}</Mono>
            </dd>
          </>
        )}
        {Object.keys(i.config ?? {}).length > 0 && (
          <>
            <dt className="text-ink-3">Konfigurasi</dt>
            <dd className="flex flex-wrap gap-2">
              {Object.entries(i.config ?? {}).map(([k, v]) => (
                <Mono key={k}>{`${k}=${typeof v === "string" ? v : Array.isArray(v) && KEPT_WHEN_OMITTED.has(k) ? `[${v.length}]` : JSON.stringify(v)}`}</Mono>
              ))}
            </dd>
          </>
        )}
        <dt className="text-ink-3">Credential (referensi)</dt>
        <dd>{i.credential_ref ? <Mono>{i.credential_ref}</Mono> : "—"} <span className="text-xs text-ink-3">nilai secret tidak pernah ditampilkan</span></dd>
        <dt className="text-ink-3">Masa berlaku credential</dt>
        <dd>
          {cap.credential?.expires_at
            ? `s.d. ${absoluteTime(cap.credential.expires_at)}`
            : cap.credential?.status === "CONFIGURED"
              ? "tidak tercatat (UNKNOWN)"
              : (cap.credential?.reason ?? "—")}
        </dd>
        <dt className="text-ink-3">Diverifikasi</dt>
        <dd>{i.last_verified_at ? <Freshness at={i.last_verified_at} /> : "belum pernah (dibuktikan Doctor/publisher)"}</dd>
        {(cap.doctor_checks ?? []).length > 0 && (
          <>
            <dt className="text-ink-3">Pemeriksaan Doctor</dt>
            <dd>
              {(cap.doctor_checks ?? []).map((c) => (
                <Mono key={c}>{c}</Mono>
              ))}
            </dd>
          </>
        )}
      </dl>
      {(cap.remedies ?? []).length > 0 && (
        <ul className="mt-3 space-y-1 text-sm">
          {(cap.remedies ?? []).map((r, idx) => (
            <li key={idx} className="flex flex-wrap items-center gap-2">
              <Terminal className="h-3.5 w-3.5 text-ink-3" aria-hidden />
              <span className="text-ink-2">{r.issue}</span>
              <span className="text-ink-3">→</span>
              <Mono>{r.command}</Mono>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- project integration settings (§04, §31)

type Values = Record<string, string | boolean>;
type Purpose = "docs" | "issues" | "code" | "ci" | "staging" | "production";

interface FieldSpec {
  key: string; // root_id | tenant_id | credential_ref | config.<name>
  label: string;
  hint?: string;
  placeholder?: string;
  required?: boolean;
  options?: string[];
  checkbox?: boolean;
  /** Client-side convenience only; the backend validates with the same rule and stays authoritative. */
  pattern?: RegExp;
  patternHint?: string;
  /** The backend keeps this key when a PUT omits it: an emptied field sends "" to clear the stored value. */
  clearable?: boolean;
}

export interface SectionSpec {
  provider: string;
  purpose: Purpose;
  title: string;
  fields: FieldSpec[];
  defaults: Values;
  note?: string;
  info?: string;
}

const DEPLOYMENTS = ["UNKNOWN", "CLOUD", "DATA_CENTER"];

/** config.gate_G1 … gate_G5: the project's pipeline reference (§31), exact CI job/step/commit-status names. */
function gateFields(what: string): FieldSpec[] {
  return PIPELINE_GATES.map((g) => ({
    key: `config.gate_${g}`,
    label: `Gate ${g} — nama ${what} persis`,
    hint: `${GATE_HELP[g] ?? ""} Pisahkan beberapa nama dengan koma; kosong = gate ini tidak dipetakan.`,
    placeholder: g === "G1" ? "lint, typecheck" : "",
  }));
}

function gateDefaults(): Values {
  return Object.fromEntries(PIPELINE_GATES.map((g) => [`config.gate_${g}`, ""]));
}

function deploymentTarget(purpose: "staging" | "production"): SectionSpec {
  return {
    provider: "deployment-target",
    purpose,
    title: `${PURPOSE_LABEL[purpose]} — ${PROVIDER_LABEL["deployment-target"]}`,
    fields: [
      {
        key: "root_id",
        label: "URL dasar target (https)",
        hint: ROOT_HINT["deployment-target"],
        placeholder: purpose === "staging" ? "https://staging.contoh.id" : "https://app.contoh.id",
        required: true,
        // same identity rule as the Integration Registry (portfolio.integrations: https, or loopback http)
        pattern: /^(?:https:\/\/[A-Za-z0-9.-]{1,253}|http:\/\/(?:127\.0\.0\.1|localhost))(?::\d{1,5})?(?:\/[A-Za-z0-9._~/-]{0,200})?$/,
        patternHint: "harus https://host[:port][/path] (atau http://127.0.0.1 lokal) tanpa user, password atau query",
      },
      {
        key: "config.health_path",
        label: "Path health/version (opsional)",
        hint: "Satu GET read-only ke URL dasar + path ini; respons harus menyebut commit yang berjalan. Kosong = /health.",
        placeholder: "/health",
        pattern: HEALTH_PATH,
        patternHint: "harus diawali / dan hanya berisi huruf, angka, . _ ~ / - (tanpa // atau ..)",
        clearable: true,
      },
      {
        key: "config.migration_credential_ref",
        label: "Referensi credential migrasi (opsional)",
        hint: "Referensi secret store untuk migrasi skema (mis. projects/<proyek>/db-migrator), bukan nilai. Verifier memeriksa bahwa ia terpisah dari credential runtime.",
        placeholder: "projects/<proyek>/db-migrator",
        pattern: PERSONAL_CREDENTIAL_REF,
        patternHint: "harus referensi personal/… atau projects/<proyek>/…, bukan nilai secret",
      },
      {
        key: "config.runtime_credential_ref",
        label: "Referensi credential runtime (opsional)",
        hint: "Referensi secret store yang dipakai aplikasi saat berjalan (mis. projects/<proyek>/db-app), bukan nilai.",
        placeholder: "projects/<proyek>/db-app",
        pattern: PERSONAL_CREDENTIAL_REF,
        patternHint: "harus referensi personal/… atau projects/<proyek>/…, bukan nilai secret",
      },
    ],
    defaults: { root_id: "", "config.health_path": "", "config.migration_credential_ref": "", "config.runtime_credential_ref": "" },
    info:
      purpose === "staging"
        ? "Hanya diamati (gate G6, tangkapan layar dokumen): AEOS tidak pernah men-deploy. Deploy ke target ini tetap tindakan manusia."
        : "Hanya diamati pasca-rilis (gate G8): AEOS tidak pernah men-deploy atau mempromosikan ke produksi.",
  };
}

export function sections(domain: string, defaultRepo: string): SectionSpec[] {
  if (domain === "OFFICE") {
    const atlassian = (provider: string, purpose: "docs" | "issues", rootLabel: string, rootHint: string, extra: FieldSpec[]): SectionSpec => ({
      provider,
      purpose,
      title: `${PURPOSE_LABEL[purpose]} — ${PROVIDER_LABEL[provider]}`,
      fields: [
        { key: "tenant_id", label: "Tenant Atlassian", hint: "mis. perusahaan.atlassian.net (Cloud)", placeholder: "perusahaan.atlassian.net", required: true },
        ...extra,
        { key: "root_id", label: rootLabel, hint: rootHint, required: true },
        { key: "config.deployment", label: "Deployment", hint: "Cloud vs Data Center harus diverifikasi (§04); Data Center tidak didukung adapter ini.", options: DEPLOYMENTS },
        { key: "config.enabled", label: "Aktifkan adapter (tetap butuh otorisasi perusahaan + interlock)", checkbox: true },
        { key: "credential_ref", label: "Referensi credential", hint: "office/… atau projects/… — bukan nilai secret", placeholder: "office/atlassian" },
      ],
      defaults: { tenant_id: "", root_id: "", "config.deployment": "UNKNOWN", "config.enabled": false, credential_ref: "office/atlassian", "config.space_id": "" },
      note: "OFFICE default deny (§23): tidak ada tulis sebelum otorisasi perusahaan tercatat.",
    });
    return [
      atlassian("confluence", "docs", "CONFLUENCE_PROJECTS_PARENT_ID", ROOT_HINT.confluence, [
        {
          key: "config.space_id",
          label: "Space ID Confluence (angka)",
          required: true,
          placeholder: "98765",
          hint: "ID numerik space (REST v2), bukan kunci space seperti ENG: buka https://<situs>.atlassian.net/wiki/api/v2/spaces?keys=<KUNCI> lalu salin \"id\".",
        },
      ]),
      atlassian("jira", "issues", "Kunci proyek Jira", ROOT_HINT.jira, []),
      {
        provider: "bitbucket",
        purpose: "code",
        title: `${PURPOSE_LABEL.code} — ${PROVIDER_LABEL.bitbucket}`,
        fields: [
          {
            key: "root_id",
            label: "Repository Bitbucket (workspace/repo_slug)",
            hint: ROOT_HINT.bitbucket,
            placeholder: "workspace/nama-repo",
            required: true,
            // same identity rule as the Integration Registry (portfolio.integrations _ROOT_PATTERNS bitbucket)
            pattern: /^[A-Za-z0-9][A-Za-z0-9_-]{0,61}\/[A-Za-z0-9._-]{1,100}$/,
            patternHint: "harus workspace/repo_slug, mis. perusahaan/aplikasi",
          },
          { key: "config.enabled", label: "Aktifkan adapter (tetap butuh otorisasi perusahaan + interlock)", checkbox: true },
          { key: "credential_ref", label: "Referensi credential", hint: "office/… atau projects/… — bukan nilai secret", placeholder: "office/bitbucket" },
        ],
        defaults: { root_id: "", "config.enabled": false, credential_ref: "office/bitbucket" },
        note: "OFFICE default deny (§23): push/PR hanya setelah otorisasi perusahaan yang mencantumkan bitbucket dan target ini, write enablement, persetujuan rencana dan interlock.",
        info:
          "Deployment Bitbucket (Cloud vs Data Center) tidak diatur di sini: catat override atlassian_deployment pada keputusan otorisasi perusahaan (Keputusan → POLICY). Hanya Bitbucket Cloud yang didukung; Data Center tidak didukung. Root ini harus sama dengan repository terdaftar yang sudah VERIFIED.",
      },
      {
        provider: "bitbucket-pipelines",
        purpose: "ci",
        title: `${PURPOSE_LABEL.ci} — ${PROVIDER_LABEL["bitbucket-pipelines"]}`,
        fields: gateFields("step Pipelines / commit status"),
        defaults: gateDefaults(),
        info: "Pipeline reference proyek (§31): gate delivery membaca status CI Bitbucket dengan peta ini (PIPELINE_REFERENCE). Tanpa peta, nama job dicocokkan dengan heuristik dan bukti CI menyebut NAME_HEURISTIC.",
      },
    ];
  }
  return [
    {
      provider: "google-drive",
      purpose: "docs",
      title: `${PURPOSE_LABEL.docs} — ${PROVIDER_LABEL["google-drive"]}`,
      fields: [
        { key: "root_id", label: "ID folder root Google Drive (opsional)", hint: "Kosong = folder proyek dibuat di My Drive (scope drive.file).", placeholder: "kosong = My Drive" },
        { key: "credential_ref", label: "Referensi credential", hint: "Token OAuth dari aeos publish google-connect — bukan nilai secret", placeholder: "personal/google-oauth-token" },
      ],
      defaults: { root_id: "", credential_ref: "personal/google-oauth-token" },
    },
    {
      provider: "github-issues",
      purpose: "issues",
      title: `${PURPOSE_LABEL.issues} — ${PROVIDER_LABEL["github-issues"]}`,
      fields: [
        { key: "root_id", label: "Repository GitHub Issues", hint: ROOT_HINT["github-issues"], placeholder: "owner/nama", required: true },
        { key: "config.issue_granularity", label: "Satu issue per", options: ["task", "feature"] },
        { key: "credential_ref", label: "Referensi credential", hint: "personal/… atau projects/… — bukan nilai secret", placeholder: "personal/github" },
      ],
      defaults: { root_id: defaultRepo, "config.issue_granularity": "task", credential_ref: "personal/github" },
    },
    {
      provider: "github-actions",
      purpose: "ci",
      title: `${PURPOSE_LABEL.ci} — ${PROVIDER_LABEL["github-actions"]}`,
      fields: gateFields("job (check-run) GitHub Actions"),
      defaults: gateDefaults(),
      info: "Pipeline reference proyek (§31). Tanpa peta, tag [G1,G2] pada nama job di workflow CI dipakai; selain itu heuristik nama (bukti CI menyebut NAME_HEURISTIC).",
    },
    deploymentTarget("staging"),
    deploymentTarget("production"),
  ];
}

/**
 * A config key naming a target (legacy alias such as config.repo on an old github-issues row): the Integration
 * Registry refuses it (TARGET_IN_CONFIG, backend portfolio.integrations._TARGET_KEY), because the exact target lives
 * only in root_id (§04). It is dropped on re-save instead of making every save of that integration fail.
 */
const TARGET_ALIAS_KEY = /(parent|root|folder|target|alias|page[_-]?id|repo|project[_-]?key)/i;

/**
 * The stored config a form sends back unchanged: never the controller-written proof (_verification) or derived
 * keys, never a legacy target alias, and only scalar values (the backend accepts nothing else).
 */
export function editableConfig(config: Record<string, unknown> | undefined): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(config ?? {}).filter(
      ([k, v]) => !k.startsWith("_") && !TARGET_ALIAS_KEY.test(k) && (v === null || ["string", "number", "boolean"].includes(typeof v)),
    ),
  );
}

function valuesFrom(spec: SectionSpec, existing: IntegrationView | undefined): Values {
  const v: Values = { ...spec.defaults };
  if (!existing) return v;
  if (existing.root_id) v.root_id = existing.root_id;
  if (existing.tenant_id) v.tenant_id = existing.tenant_id;
  if (existing.credential_ref) v.credential_ref = existing.credential_ref;
  for (const [k, val] of Object.entries(existing.config ?? {})) {
    if (`config.${k}` in v) v[`config.${k}`] = typeof val === "boolean" ? val : String(val ?? "");
  }
  return v;
}

/**
 * PUT body for one target: credential references only, never a secret value (§26).
 * The stored config is kept: keys this section does not edit are sent back unchanged (a re-save never wipes
 * them); an emptied text field removes its key.
 */
export function integrationBody(spec: SectionSpec, values: Values, existingConfig?: Record<string, unknown>): Record<string, unknown> {
  const config: Record<string, unknown> = editableConfig(existingConfig);
  const body: Record<string, unknown> = { provider: spec.provider, purpose: spec.purpose };
  for (const f of spec.fields) {
    const raw = values[f.key];
    if (f.key.startsWith("config.")) {
      const name = f.key.slice("config.".length);
      if (f.checkbox) config[name] = raw === true;
      else if (typeof raw === "string" && raw.trim()) config[name] = raw.trim();
      // a key the backend keeps when omitted is cleared explicitly with "" (only when a value is stored)
      else if (f.clearable && existingConfig && name in existingConfig) config[name] = "";
      else delete config[name];
    } else {
      body[f.key] = typeof raw === "string" && raw.trim() ? raw.trim() : null;
    }
  }
  body.config = config;
  return body;
}

function TargetSection({ project, spec, existing, onSaved }: { project: ProjectView; spec: SectionSpec; existing?: IntegrationView; onSaved: () => void }) {
  const [values, setValues] = useState<Values>(() => valuesFrom(spec, existing));
  // Reset the form only when the stored integration itself changes (not on every refetch of the list).
  const savedAt = existing ? `${existing.id}:${existing.updated_at ?? ""}` : "";
  const [seen, setSeen] = useState(savedAt);
  if (seen !== savedAt) {
    setSeen(savedAt);
    setValues(valuesFrom(spec, existing));
  }
  const save = useMutation({
    mutationFn: () => put(`/api/projects/${project.id}/integrations`, integrationBody(spec, values, existing?.config)),
    onSuccess: onSaved,
  });
  const missing = spec.fields.filter((f) => f.required && !String(values[f.key] ?? "").trim());
  const invalid = spec.fields.filter((f) => {
    const v = String(values[f.key] ?? "").trim();
    return !!f.pattern && !!v && !f.pattern.test(v);
  });
  const invalidKeys = new Set(invalid.map((f) => f.key));
  return (
    <form
      aria-label={spec.title}
      className="grid gap-3 rounded-md border border-line p-3 sm:grid-cols-2"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <div className="sm:col-span-2 flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">{spec.title}</span>
        <Mono>{`${spec.provider}/${spec.purpose}`}</Mono>
        {existing ? <StatusBadge status="CONFIGURED" label="Tersimpan (belum terverifikasi)" /> : <StatusBadge status="MISSING" label="Belum diatur" />}
      </div>
      {spec.note && (
        <div className="sm:col-span-2">
          <Notice tone="warn">{spec.note}</Notice>
        </div>
      )}
      {spec.info && (
        <div className="sm:col-span-2">
          <Notice tone="info">{spec.info}</Notice>
        </div>
      )}
      {spec.fields.map((f) =>
        f.checkbox ? (
          <label key={f.key} className="flex items-center gap-2 text-sm sm:col-span-2">
            <input type="checkbox" checked={values[f.key] === true} onChange={(e) => setValues({ ...values, [f.key]: e.target.checked })} /> {f.label}
          </label>
        ) : (
          <Field key={f.key} label={f.label} hint={f.hint}>
            {f.options ? (
              <select className={inputClass} value={String(values[f.key] ?? "")} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}>
                {f.options.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            ) : (
              <input
                className={inputClass}
                placeholder={f.placeholder}
                aria-invalid={invalidKeys.has(f.key) || undefined}
                value={String(values[f.key] ?? "")}
                onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
              />
            )}
          </Field>
        ),
      )}
      {save.error ? (
        <div className="sm:col-span-2">
          <ErrorBox error={save.error} />
        </div>
      ) : null}
      <div className="sm:col-span-2">
        <Button type="submit" variant="primary" busy={save.isPending} disabled={missing.length > 0 || invalid.length > 0}>
          Simpan {PROVIDER_LABEL[spec.provider] ?? spec.provider}
          {spec.provider === "deployment-target" ? ` (${PURPOSE_LABEL[spec.purpose]})` : ""}
        </Button>
        {missing.length > 0 && <span className="ml-2 text-xs text-warn">Wajib: {missing.map((f) => f.label).join(", ")}</span>}
        {/* always rendered, so assistive technology announces the message when it appears */}
        <span aria-live="polite" className="ml-2 text-xs text-bad">
          {invalid.length > 0 ? `Format tidak valid: ${invalid.map((f) => `${f.label} (${f.patternHint ?? "format"})`).join(", ")}` : ""}
        </span>
      </div>
    </form>
  );
}

export function IntegrationSettings({ project, integrations, onSaved }: { project: ProjectView; integrations: IntegrationView[]; onSaved: () => void }) {
  const repo = (project.repositories ?? []).find((r) => r.provider?.toUpperCase() === "GITHUB")?.full_name ?? "";
  const specs = useMemo(() => sections(project.domain, repo), [project.domain, repo]);
  return (
    <Card title="Pengaturan integrasi proyek">
      <div className="grid gap-3">
        <Notice tone="info">
          Target dokumentasi/tracking menjadi identitas persis <Mono>{"{provider, root_id}"}</Mono> yang Anda setujui saat Approve &amp; Publish; target kode,
          CI dan deployment dipakai delivery (PR, gate) dengan aturan yang sama. Hanya referensi credential yang disimpan; nilai secret disimpan dengan{" "}
          <Mono>aeos secrets set</Mono> atau <Mono>aeos publish google-connect</Mono>.
        </Notice>
        {specs.map((s) => (
          <TargetSection key={`${s.provider}:${s.purpose}`} project={project} spec={s} existing={integrations.find((i) => i.provider === s.provider && i.purpose === s.purpose)} onSaved={onSaved} />
        ))}
      </div>
    </Card>
  );
}

function IntegrationForm({ project, allowed, integrations, onSaved }: { project: ProjectView; allowed: Record<string, string[]>; integrations: IntegrationView[]; onSaved: () => void }) {
  const providers = Object.keys(allowed);
  const [provider, setProvider] = useState(providers[0] ?? "");
  const [purpose, setPurpose] = useState(allowed[providers[0]]?.[0] ?? "");
  const [rootId, setRootId] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [credentialRef, setCredentialRef] = useState("");
  const [healthPath, setHealthPath] = useState("");
  useEffect(() => {
    setPurpose(allowed[provider]?.[0] ?? "");
  }, [provider, allowed]);
  // The stored integration for this provider + purpose: its identifiers prefill the form and its config is sent back
  // unchanged, so saving here never wipes config.enabled, the pipeline reference or the health path.
  const existing = integrations.find((i) => i.provider === provider && i.purpose === purpose);
  const existingKey = existing ? `${existing.id}:${existing.updated_at ?? ""}` : `new:${provider}:${purpose}`;
  const [seenKey, setSeenKey] = useState("");
  if (seenKey !== existingKey) {
    // reset only when the selected stored integration changes (not on every refetch of the list)
    setSeenKey(existingKey);
    setRootId(existing?.root_id ?? "");
    setTenantId(existing?.tenant_id ?? "");
    setCredentialRef(existing?.credential_ref ?? "");
    setHealthPath(typeof existing?.config?.health_path === "string" ? existing.config.health_path : "");
  }
  const keptConfig = editableConfig(existing?.config);
  const isTarget = provider === "deployment-target";
  const storedHealth = typeof existing?.config?.health_path === "string" ? existing.config.health_path : null;
  const healthOk = !healthPath.trim() || targetPathOk(healthPath.trim());
  // health_path is kept by the backend when omitted: a typed value replaces it, an emptied field sends "" to clear it
  const sentConfig: Record<string, unknown> = { ...keptConfig };
  if (isTarget) {
    if (healthPath.trim()) sentConfig.health_path = healthPath.trim();
    else if (storedHealth !== null) sentConfig.health_path = "";
    else delete sentConfig.health_path;
  }
  const save = useMutation({
    mutationFn: () =>
      put(`/api/projects/${project.id}/integrations`, {
        provider,
        purpose,
        root_id: rootId.trim() || null,
        tenant_id: tenantId.trim() || null,
        credential_ref: credentialRef.trim() || null,
        config: sentConfig,
      }),
    onSuccess: onSaved,
  });
  return (
    <Card title="Atur integrasi lain">
      <form
        className="grid gap-3 sm:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Provider" hint={`Hanya provider ${project.domain}`}>
          <select className={inputClass} value={provider} onChange={(e) => setProvider(e.target.value)}>
            {providers.map((p) => (
              <option key={p} value={p}>
                {PROVIDER_LABEL[p] ? `${PROVIDER_LABEL[p]} (${p})` : p}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Tujuan">
          <select className={inputClass} value={purpose} onChange={(e) => setPurpose(e.target.value)}>
            {(allowed[provider] ?? []).map((p) => (
              <option key={p} value={p}>
                {PURPOSE_LABEL[p] ?? p}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Root ID" hint={ROOT_HINT[provider] ?? "ID target yang sah"}>
          <input className={inputClass} value={rootId} onChange={(e) => setRootId(e.target.value)} />
        </Field>
        <Field label="Tenant (opsional)" hint="mis. perusahaan.atlassian.net">
          <input className={inputClass} value={tenantId} onChange={(e) => setTenantId(e.target.value)} />
        </Field>
        <Field label="Referensi credential" hint={project.domain === "OFFICE" ? "office/… atau projects/…" : "personal/… atau projects/… (bukan nilai secret)"}>
          <input className={inputClass} value={credentialRef} onChange={(e) => setCredentialRef(e.target.value)} placeholder="personal/github" />
        </Field>
        {isTarget && (
          <Field label="Path health/version (opsional)" hint="Kosong = /health. Mengosongkan path tersimpan mengirim nilai kosong agar kembali ke /health.">
            <input
              className={`${inputClass} mono`}
              value={healthPath}
              aria-invalid={!healthOk || undefined}
              onChange={(e) => setHealthPath(e.target.value)}
              placeholder="/health"
            />
          </Field>
        )}
        <div className="text-xs text-ink-3 sm:col-span-2">
          {Object.keys(keptConfig).filter((k) => !(isTarget && k === "health_path")).length > 0 ? (
            <>
              Konfigurasi tersimpan dipertahankan apa adanya:{" "}
              <Mono>{Object.keys(keptConfig).filter((k) => !(isTarget && k === "health_path")).join(", ")}</Mono>. Ubah lewat bagian pengaturan di atas.
            </>
          ) : (
            "Belum ada konfigurasi tersimpan untuk provider + tujuan ini."
          )}
        </div>
        {save.error ? (
          <div className="sm:col-span-2">
            <ErrorBox error={save.error} />
          </div>
        ) : null}
        <div className="sm:col-span-2">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={!provider || !purpose || !healthOk}>
            Simpan konfigurasi
          </Button>
          <span aria-live="polite" className="ml-2 text-xs text-bad">
            {!healthOk ? "Path health harus path absolut seperti /health (tanpa host, query, // atau ..)." : ""}
          </span>
        </div>
      </form>
    </Card>
  );
}

export function IntegrationsPanel({ project, isAdmin }: { project: ProjectView; isAdmin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["integrations", project.id], queryFn: () => get<IntegrationList>(`/api/projects/${project.id}/integrations`) });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["integrations", project.id] });
    qc.invalidateQueries({ queryKey: ["project", project.id] });
    qc.invalidateQueries({ queryKey: ["projects"] });
  };
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const items = q.data?.items ?? [];
  return (
    <div className="space-y-4">
      <Notice>
        Status di sini hanya konfigurasi (root ID dan referensi credential). Koneksi dan fungsi dibuktikan oleh Doctor/publisher — “terkonfigurasi” bukan
        “terverifikasi”.
        {project.domain === "PERSONAL" ? (
          <>
            {" "}
            Hubungkan Google Drive dengan <Mono>aeos publish google-connect</Mono>.
          </>
        ) : (
          " Integrasi OFFICE tetap ditolak sampai ada otorisasi perusahaan yang tercatat."
        )}
      </Notice>
      {items.length === 0 ? (
        <EmptyState title="Belum ada integrasi">
          {project.domain === "PERSONAL"
            ? "Atur Google Drive (dokumentasi), GitHub Issues (tracking kerja), pipeline reference GitHub Actions dan target staging/produksi di bawah, lalu jalankan aeos publish google-connect."
            : "Atur Confluence (CONFLUENCE_PROJECTS_PARENT_ID), Jira, kode Bitbucket dan pipeline reference Bitbucket Pipelines setelah otorisasi kantor."}
        </EmptyState>
      ) : (
        items.map((i) => <IntegrationRow key={i.id} i={i} />)
      )}
      {project.domain === "PERSONAL" && q.data && <DeploymentTargetsPanel project={project} integrations={items} isAdmin={isAdmin} />}
      {isAdmin && q.data && <IntegrationSettings project={project} integrations={items} onSaved={refresh} />}
      {isAdmin && q.data && <IntegrationForm project={project} allowed={q.data.allowed_providers} integrations={items} onSaved={refresh} />}
    </div>
  );
}
