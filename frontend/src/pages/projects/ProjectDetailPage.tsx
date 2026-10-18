import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ClipboardCheck, FileStack, GitBranch, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { ApiError, get, patch } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button, Card, EmptyState, ErrorBox, Field, inputClass, KeyValue, Loading, Mono, Notice, PageHeader, Tabs } from "../../components/ui";
import { absoluteTime } from "../../lib/format";
import { AttachmentsPanel } from "./AttachmentsPanel";
import { ClassificationBadge, DomainBadge, ModuleBadge, PlatformBadge } from "./badges";
import { ContextPanel } from "./ContextPanel";
import { IntegrationsPanel } from "./IntegrationsPanel";
import { MemoryPanel } from "./MemoryPanel";
import { RepositoriesPanel } from "./RepositoriesPanel";
import { SearchPanel } from "./SearchPanel";
import type { ProjectView } from "./types";

const TRANSITIONS: Record<string, string[]> = {
  ACTIVE: ["ARCHIVED", "RETIRING"],
  ARCHIVED: ["ACTIVE", "RETIRING"],
  RETIRING: ["ACTIVE", "RETIRED"],
  RETIRED: [],
};
const LIFECYCLE_LABEL: Record<string, string> = { ACTIVE: "Aktifkan", ARCHIVED: "Arsipkan", RETIRING: "Mulai pensiun", RETIRED: "Pensiunkan" };

function Overview({ project, isAdmin }: { project: ProjectView; isAdmin: boolean }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description ?? "");
  const [idea, setIdea] = useState(project.idea ?? "");
  const done = () => {
    qc.invalidateQueries({ queryKey: ["project", project.id] });
    qc.invalidateQueries({ queryKey: ["projects"] });
  };
  const save = useMutation({
    mutationFn: (body: Record<string, string>) => patch(`/api/projects/${project.id}`, body),
    onSuccess: () => {
      setEditing(false);
      done();
    },
  });
  const changeLifecycle = (target: string) => {
    const reason = window.prompt(`Alasan perubahan siklus hidup ke ${target}:`, "");
    if (reason === null) return;
    save.mutate({ lifecycle: target, lifecycle_reason: reason });
  };
  const findings = project.input_findings ?? [];
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card title="Identitas" className="lg:col-span-1">
        <KeyValue
          items={[
            ["ID", <Mono key="id">{project.id}</Mono>],
            ["Kunci", <Mono key="k">{project.project_key}</Mono>],
            ["Domain", <DomainBadge key="d" domain={project.domain} />],
            ["Klasifikasi", <ClassificationBadge key="c" value={project.data_classification} />],
            ["Siklus hidup", <ModuleBadge key="l" kind="lifecycle" value={project.lifecycle} />],
            ["Jenis", project.project_kind === "EXISTING" ? "Existing" : "Baru"],
            ["Platform", project.is_platform ? <PlatformBadge key="p" /> : "Bukan"],
            ["Dibuat", absoluteTime(project.created_at)],
            ["Diperbarui", absoluteTime(project.updated_at)],
          ]}
        />
        {project.lifecycle_reason && <p className="mt-2 text-xs text-ink-3">Alasan siklus hidup: {project.lifecycle_reason}</p>}
        <p className="mt-3 text-xs text-ink-3">Domain tidak dapat diubah; pemindahan domain adalah migrasi dengan keputusan pemilik.</p>
        {isAdmin && (TRANSITIONS[project.lifecycle] ?? []).length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {(TRANSITIONS[project.lifecycle] ?? []).map((t) => (
              <Button key={t} onClick={() => changeLifecycle(t)} busy={save.isPending} variant={t === "RETIRED" ? "danger" : "secondary"}>
                {LIFECYCLE_LABEL[t]}
              </Button>
            ))}
          </div>
        )}
      </Card>
      <Card
        title="Ide dan deskripsi"
        className="lg:col-span-2"
        actions={
          isAdmin && !editing ? (
            <Button
              variant="ghost"
              onClick={() => {
                // start from the latest saved values, not from what was loaded when the page opened
                setName(project.name);
                setDescription(project.description ?? "");
                setIdea(project.idea ?? "");
                setEditing(true);
              }}
            >
              Ubah
            </Button>
          ) : undefined
        }
      >
        {editing ? (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate({ name, description, idea });
            }}
          >
            <Field label="Nama">
              <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
            </Field>
            <Field label="Deskripsi">
              <input className={inputClass} value={description} onChange={(e) => setDescription(e.target.value)} maxLength={5000} />
            </Field>
            <Field label="Ide">
              <textarea className={inputClass + " min-h-28"} value={idea} onChange={(e) => setIdea(e.target.value)} maxLength={20000} />
            </Field>
            <div className="flex gap-2">
              <Button type="submit" variant="primary" busy={save.isPending}>
                Simpan
              </Button>
              <Button type="button" onClick={() => setEditing(false)}>
                Batal
              </Button>
            </div>
          </form>
        ) : (
          <div className="space-y-2 text-sm">
            <p className="text-ink-2">{project.description || "Belum ada deskripsi."}</p>
            <p className="whitespace-pre-wrap">{project.idea || "Belum ada ide tercatat."}</p>
          </div>
        )}
        {save.error ? (
          <div className="mt-3">
            <ErrorBox error={save.error} />
          </div>
        ) : null}
        {findings.length > 0 && (
          <div className="mt-4">
            <Notice tone="warn">
              Teks masukan berisi {findings.length} temuan (instruksi/secret). Dicatat sebagai DATA — tidak mengubah domain, kebijakan, izin atau routing.
            </Notice>
            <ul className="mt-2 space-y-1 text-xs">
              {findings.map((f, i) => (
                <li key={i} className="flex items-start gap-1 text-warn">
                  <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                  <span>
                    <strong>{f.kind}</strong> {f.marker ? `· ${f.marker}` : ""} {f.source ? `(${f.source})` : ""} {f.excerpt ? <Mono>{f.excerpt}</Mono> : null}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Card>
      <div className="lg:col-span-3">
        <SearchPanel projectId={project.id} />
      </div>
    </div>
  );
}

const TAB_IDS = ["ringkasan", "repositories", "integrations", "attachments", "context", "memory"];

export default function ProjectDetailPage() {
  const { projectId = "" } = useParams();
  const { isAdmin, domain, projectId: activeProject, selectContext } = useAuth();
  const location = useLocation();
  const [ctxError, setCtxError] = useState<unknown>(null);
  const q = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => get<{ project: ProjectView; checked_at: string; mode: string; source: string }>(`/api/projects/${projectId}`),
    retry: (count, e) => !(e instanceof ApiError && e.status === 404) && count < 1,
  });
  if (q.isLoading) return <Loading />;
  if (q.error) {
    const notFound = q.error instanceof ApiError && q.error.status === 404;
    return (
      <div>
        <PageHeader title="Detail proyek" question="Konteks, rencana, dokumen dan integrasi proyek" />
        {notFound ? (
          <EmptyState title={`Proyek tidak ditemukan di domain ${domain}`}>
            Proyek dari domain lain tidak ditampilkan. <Link to="/projects" className="text-accent hover:underline">Kembali ke portofolio</Link>
          </EmptyState>
        ) : (
          <ErrorBox error={q.error} />
        )}
      </div>
    );
  }
  const project = q.data!.project;
  const hash = location.hash.replace("#", "");
  const initial = TAB_IDS.includes(hash) ? hash : "ringkasan";
  const isActive = activeProject === project.id;
  return (
    <div className="space-y-5">
      <PageHeader
        title={project.name}
        question="Konteks, rencana, dokumen dan integrasi proyek"
        source={q.data!.source}
        checkedAt={q.data!.checked_at}
        evidenceLimit="Repository/integrasi menampilkan status terakhir yang tercatat; lampiran adalah DATA"
        mode={project.mode}
        actions={
          <>
            <Button
              variant={isActive ? "secondary" : "primary"}
              disabled={isActive}
              onClick={async () => {
                setCtxError(null);
                try {
                  await selectContext(project.domain, project.id);
                } catch (e) {
                  setCtxError(e);
                }
              }}
            >
              <CheckCircle2 className="h-4 w-4" aria-hidden /> {isActive ? "Proyek aktif" : "Jadikan proyek aktif"}
            </Button>
            <Link to={project.links.plans} className="inline-flex items-center gap-1 rounded-md border border-line bg-surface px-3 py-1.5 text-sm hover:bg-surface-3">
              <ClipboardCheck className="h-4 w-4" aria-hidden /> Rencana
            </Link>
            <Link to={project.links.publications} className="inline-flex items-center gap-1 rounded-md border border-line bg-surface px-3 py-1.5 text-sm hover:bg-surface-3">
              <FileStack className="h-4 w-4" aria-hidden /> Publikasi
            </Link>
            <Link to={project.links.dag} className="inline-flex items-center gap-1 rounded-md border border-line bg-surface px-3 py-1.5 text-sm hover:bg-surface-3">
              <GitBranch className="h-4 w-4" aria-hidden /> Task DAG
            </Link>
          </>
        }
      />
      <div className="flex flex-wrap items-center gap-2">
        <DomainBadge domain={project.domain} />
        <ModuleBadge kind="lifecycle" value={project.lifecycle} />
        <ClassificationBadge value={project.data_classification} />
        {project.is_platform && <PlatformBadge />}
        <span className="text-xs text-ink-3">
          Dokumen: <ModuleBadge kind="config" value={project.integration_state.docs} /> · Issue:{" "}
          <ModuleBadge kind="config" value={project.integration_state.issues} />
        </span>
      </div>
      {ctxError ? <ErrorBox error={ctxError} /> : null}
      <Tabs
        key={initial}
        initial={initial}
        tabs={[
          { id: "ringkasan", label: "Ringkasan", content: <Overview project={project} isAdmin={isAdmin} /> },
          { id: "repositories", label: `Repository (${project.repositories.length})`, content: <RepositoriesPanel project={project} isAdmin={isAdmin} /> },
          { id: "integrations", label: "Integrasi", content: <IntegrationsPanel project={project} isAdmin={isAdmin} /> },
          { id: "attachments", label: `Lampiran (${project.attachments?.total ?? 0})`, content: <AttachmentsPanel projectId={project.id} isAdmin={isAdmin} /> },
          { id: "context", label: "Konteks", content: <ContextPanel project={project} isAdmin={isAdmin} /> },
          { id: "memory", label: "Memori", content: <MemoryPanel project={project} isAdmin={isAdmin} /> },
        ]}
      />
    </div>
  );
}
