import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Terminal } from "lucide-react";
import { useEffect, useState } from "react";
import { get, put } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorBox, Field, Freshness, inputClass, Loading, Mono, Notice } from "../../components/ui";
import { ModuleBadge } from "./badges";
import type { IntegrationView, ListResponse, ProjectView } from "./types";

const PURPOSE_LABEL: Record<string, string> = { docs: "Dokumentasi", issues: "Tracking kerja", code: "Kode", ci: "CI", roadmap: "Roadmap" };
const ROOT_HINT: Record<string, string> = {
  GOOGLE_DRIVE: "ID folder Google Drive proyek (GOOGLE_DRIVE_FOLDER_ID)",
  GOOGLE_DOCS: "ID folder Google Drive proyek",
  CONFLUENCE: "CONFLUENCE_PROJECTS_PARENT_ID (angka)",
  JIRA: "Kunci proyek Jira, mis. AEOS",
  GITHUB_ISSUES: "owner/nama repository",
  GITHUB: "owner/nama repository",
  BITBUCKET: "workspace/nama repository",
};

interface IntegrationList extends ListResponse<IntegrationView> {
  allowed_providers: Record<string, string[]>;
}

function IntegrationRow({ i }: { i: IntegrationView }) {
  const cap = i.capability_status ?? {};
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{i.provider}</span>
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
        <dd>{i.root_id ? <Mono>{i.root_id}</Mono> : <span className="text-warn">belum diatur</span>}</dd>
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
        <dt className="text-ink-3">Diverifikasi</dt>
        <dd>{i.last_verified_at ? <Freshness at={i.last_verified_at} /> : "belum pernah (dibuktikan Doctor/publisher)"}</dd>
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
    <Card title="Atur integrasi">
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
              <option key={p}>{p}</option>
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
            ? "Atur GOOGLE_DRIVE (docs) dan GITHUB_ISSUES (issues) di bawah, lalu jalankan aeos publish google-connect."
            : "Atur CONFLUENCE (CONFLUENCE_PROJECTS_PARENT_ID) dan JIRA setelah otorisasi kantor."}
        </EmptyState>
      ) : (
        items.map((i) => <IntegrationRow key={i.id} i={i} />)
      )}
      {isAdmin && q.data && <IntegrationForm project={project} allowed={q.data.allowed_providers} onSaved={refresh} />}
    </div>
  );
}
