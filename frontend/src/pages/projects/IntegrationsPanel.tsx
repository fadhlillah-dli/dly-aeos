import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Terminal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { get, put } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, Freshness, inputClass, Loading, Mono, Notice } from "../../components/ui";
import { absoluteTime } from "../../lib/format";
import { ModuleBadge } from "./badges";
import type { IntegrationView, ListResponse, ProjectView } from "./types";

const PURPOSE_LABEL: Record<string, string> = { docs: "Dokumentasi", issues: "Tracking kerja", code: "Kode", ci: "CI", roadmap: "Roadmap" };
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
};
const ROOT_HINT: Record<string, string> = {
  "google-drive": "ID folder Google Drive (GOOGLE_DRIVE_FOLDER_ID), opsional",
  confluence: "CONFLUENCE_PROJECTS_PARENT_ID (angka)",
  jira: "Kunci proyek Jira (JIRA_PROJECT_KEY), mis. AEOS",
  "github-issues": "owner/nama repository (GITHUB_REPOSITORY)",
  "github-repo": "owner/nama repository",
  bitbucket: "workspace/nama repository",
};

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

// ---------------------------------------------------------------- publication target settings (§04)

type Values = Record<string, string | boolean>;

interface FieldSpec {
  key: string; // root_id | tenant_id | credential_ref | config.<name>
  label: string;
  hint?: string;
  placeholder?: string;
  required?: boolean;
  options?: string[];
  checkbox?: boolean;
}

interface SectionSpec {
  provider: string;
  purpose: "docs" | "issues";
  title: string;
  fields: FieldSpec[];
  defaults: Values;
  note?: string;
}

const DEPLOYMENTS = ["UNKNOWN", "CLOUD", "DATA_CENTER"];

function sections(domain: string, defaultRepo: string): SectionSpec[] {
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
        { key: "config.space_id", label: "Space ID Confluence", required: true, placeholder: "ENG" },
      ]),
      atlassian("jira", "issues", "Kunci proyek Jira", ROOT_HINT.jira, []),
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
  ];
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

/** PUT body for one target: credential references only, never a secret value (§26). */
export function integrationBody(spec: SectionSpec, values: Values): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  const body: Record<string, unknown> = { provider: spec.provider, purpose: spec.purpose };
  for (const f of spec.fields) {
    const raw = values[f.key];
    if (f.key.startsWith("config.")) {
      const name = f.key.slice("config.".length);
      if (f.checkbox) config[name] = raw === true;
      else if (typeof raw === "string" && raw.trim()) config[name] = raw.trim();
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
  const save = useMutation({ mutationFn: () => put(`/api/projects/${project.id}/integrations`, integrationBody(spec, values)), onSuccess: onSaved });
  const missing = spec.fields.filter((f) => f.required && !String(values[f.key] ?? "").trim());
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
        <Mono>{spec.provider}</Mono>
        {existing ? <StatusBadge status="CONFIGURED" label="Tersimpan (belum terverifikasi)" /> : <StatusBadge status="MISSING" label="Belum diatur" />}
      </div>
      {spec.note && (
        <div className="sm:col-span-2">
          <Notice tone="warn">{spec.note}</Notice>
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
              <input className={inputClass} placeholder={f.placeholder} value={String(values[f.key] ?? "")} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
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
        <Button type="submit" variant="primary" busy={save.isPending} disabled={missing.length > 0}>
          Simpan {PROVIDER_LABEL[spec.provider] ?? spec.provider}
        </Button>
        {missing.length > 0 && <span className="ml-2 text-xs text-warn">Wajib: {missing.map((f) => f.label).join(", ")}</span>}
      </div>
    </form>
  );
}

export function IntegrationSettings({ project, integrations, onSaved }: { project: ProjectView; integrations: IntegrationView[]; onSaved: () => void }) {
  const repo = (project.repositories ?? []).find((r) => r.provider?.toUpperCase() === "GITHUB")?.full_name ?? "";
  const specs = useMemo(() => sections(project.domain, repo), [project.domain, repo]);
  return (
    <Card title="Pengaturan integrasi publikasi">
      <div className="grid gap-3">
        <Notice tone="info">
          Target ini menjadi identitas persis <Mono>{"{provider, root_id}"}</Mono> yang Anda setujui saat Approve &amp; Publish. Hanya referensi credential
          yang disimpan; nilai secret disimpan dengan <Mono>aeos secrets set</Mono> atau <Mono>aeos publish google-connect</Mono>.
        </Notice>
        {specs.map((s) => (
          <TargetSection key={`${s.provider}:${s.purpose}`} project={project} spec={s} existing={integrations.find((i) => i.provider === s.provider && i.purpose === s.purpose)} onSaved={onSaved} />
        ))}
      </div>
    </Card>
  );
}

function IntegrationForm({ project, allowed, onSaved }: { project: ProjectView; allowed: Record<string, string[]>; onSaved: () => void }) {
  const providers = Object.keys(allowed);
  const [provider, setProvider] = useState(providers[0] ?? "");
  const [purpose, setPurpose] = useState(allowed[providers[0]]?.[0] ?? "");
  const [rootId, setRootId] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [credentialRef, setCredentialRef] = useState("");
  useEffect(() => {
    setPurpose(allowed[provider]?.[0] ?? "");
  }, [provider, allowed]);
  const save = useMutation({
    mutationFn: () =>
      put(`/api/projects/${project.id}/integrations`, {
        provider,
        purpose,
        root_id: rootId.trim() || null,
        tenant_id: tenantId.trim() || null,
        credential_ref: credentialRef.trim() || null,
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
        {save.error ? (
          <div className="sm:col-span-2">
            <ErrorBox error={save.error} />
          </div>
        ) : null}
        <div className="sm:col-span-2">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={!provider || !purpose}>
            Simpan konfigurasi
          </Button>
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
            ? "Atur Google Drive (dokumentasi) dan GitHub Issues (tracking kerja) di bawah, lalu jalankan aeos publish google-connect."
            : "Atur Confluence (CONFLUENCE_PROJECTS_PARENT_ID) dan Jira setelah otorisasi kantor."}
        </EmptyState>
      ) : (
        items.map((i) => <IntegrationRow key={i.id} i={i} />)
      )}
      {isAdmin && q.data && <IntegrationSettings project={project} integrations={items} onSaved={refresh} />}
      {isAdmin && q.data && <IntegrationForm project={project} allowed={q.data.allowed_providers} onSaved={refresh} />}
    </div>
  );
}
