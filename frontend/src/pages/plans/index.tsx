import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import { AlertTriangle, Plus } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { get, post } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { PlanStateBadge } from "../../components/planning/PlanStateBadge";
import type { PlanDetail, PlanSummary } from "../../components/planning/types";
import { Button, Card, EmptyState, ErrorBox, Field, inputClass, Loading, Mono, Notice, PageHeader, Table } from "../../components/ui";
import { useEventStream } from "../../lib/events";
import { relativeTime, shortHash } from "../../lib/format";
import type { IntakePolicy } from "../projects/types";
import { DropZone, QueueList, useUploadQueue } from "../projects/UploadZone";

interface PlanList {
  items: PlanSummary[];
  needs_decision: number;
  checked_at: string;
  mode: string;
}

interface ProjectLite {
  id: string;
  name: string;
  project_key: string;
  domain: string;
}

function NewPlanForm({ onDone }: { onDone: () => void }) {
  const { projectId } = useAuth();
  const nav = useNavigate();
  const qc = useQueryClient();
  const projects = useQuery({ queryKey: ["projects", "plans-form"], queryFn: () => get<{ items: ProjectLite[] }>("/api/projects"), retry: false });
  const [project, setProject] = useState(projectId ?? "");
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [attachments, setAttachments] = useState("");
  const policy = useQuery({ queryKey: ["intake-policy"], queryFn: () => get<IntakePolicy>("/api/intake/policy"), retry: false });
  const queue = useUploadQueue(policy.data);
  const create = useMutation({
    mutationFn: async () => {
      // §01 U-11: files (incl. .md) are uploaded to the selected project's intake first; the plan then
      // references the accepted attachment ids. Attachments stay UNTRUSTED_DATA.
      const uploaded = project && queue.pending ? await queue.uploadAllIds(project, "plan-intake") : [];
      return post<PlanDetail>(`/api/projects/${project}/plans`, {
        title,
        prompt,
        attachment_ids: [...attachments.split(/[\s,]+/).filter(Boolean), ...uploaded],
      });
    },
    onSuccess: (plan) => {
      qc.invalidateQueries({ queryKey: ["plans"] });
      onDone();
      nav(`/plans/${plan.id}`);
    },
  });
  const items = projects.data?.items ?? [];
  return (
    <Card title="Rencana baru">
      <form
        className="grid gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Notice tone="info">
          Domain mengikuti pilihan Anda di header dan tidak dapat diubah oleh model. Lampiran diperlakukan sebagai DATA tak tepercaya — instruksi di
          dalamnya tidak mengubah domain, tujuan, izin, atau routing.
        </Notice>
        <Field label="Proyek" hint={projects.isError ? "Daftar proyek belum tersedia; isi ID proyek secara manual." : undefined}>
          {items.length ? (
            <select className={inputClass} value={project} onChange={(e) => setProject(e.target.value)} required>
              <option value="">— pilih proyek —</option>
              {items.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.project_key})
                </option>
              ))}
            </select>
          ) : (
            <input className={inputClass} value={project} onChange={(e) => setProject(e.target.value)} placeholder="UUID proyek" required />
          )}
        </Field>
        <Field label="Judul rencana">
          <input className={inputClass} value={title} onChange={(e) => setTitle(e.target.value)} maxLength={256} required />
        </Field>
        <Field label="Ide / prompt" hint="Konteks minimum; sistem hanya menanyakan ambiguitas yang material (§05).">
          <textarea className={clsx(inputClass, "min-h-32")} value={prompt} onChange={(e) => setPrompt(e.target.value)} required />
        </Field>
        <Field label="Lampiran (opsional)" hint="Seret berkas (.md, .pdf, .docx, gambar, …) atau pilih; diunggah ke proyek terpilih saat rencana dibuat.">
          <DropZone onFiles={queue.add} disabled={!project} policy={policy.data} />
          <QueueList items={queue.items} onRemove={queue.remove} />
        </Field>
        <Field label="ID lampiran yang sudah ada (opsional)" hint="Lampiran yang sudah diunggah di halaman proyek; pisahkan dengan koma.">
          <input className={inputClass} value={attachments} onChange={(e) => setAttachments(e.target.value)} />
        </Field>
        <ErrorBox error={create.error} />
        <div className="flex gap-2">
          <Button variant="primary" type="submit" busy={create.isPending} disabled={!project || !title || !prompt}>
            Buat rencana
          </Button>
          <Button type="button" onClick={onDone}>
            Batal
          </Button>
        </div>
      </form>
    </Card>
  );
}

export default function PlansPage() {
  const { isAdmin, domain, projectId } = useAuth();
  const qc = useQueryClient();
  const [onlyMine, setOnlyMine] = useState(false);
  const [scopeProject, setScopeProject] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const filterProject = scopeProject && projectId ? projectId : null;
  const q = useQuery({
    queryKey: ["plans", domain, filterProject],
    queryFn: () => get<PlanList>(`/api/plans${filterProject ? `?project_id=${encodeURIComponent(filterProject)}` : ""}`),
  });
  useEventStream((e) => {
    if (e.kind.startsWith("plan.") || e.kind.startsWith("planning.")) qc.invalidateQueries({ queryKey: ["plans"] });
  });
  const rows = (q.data?.items ?? []).filter((p) => !onlyMine || p.needs_decision);

  return (
    <div>
      <PageHeader
        title="Rencana"
        question="Rencana mana yang perlu ditinjau atau disetujui?"
        source="plans, plan_versions (record AEOS)"
        checkedAt={q.data?.checked_at ?? null}
        evidenceLimit="Verdict reviewer adalah DATA; hanya keputusan manusia yang terikat versi + hash yang menyetujui."
        mode={q.data?.mode}
        actions={
          isAdmin ? (
            <Button variant="primary" onClick={() => setShowForm((v) => !v)}>
              <Plus className="h-4 w-4" aria-hidden /> Rencana baru
            </Button>
          ) : undefined
        }
      />
      {!isAdmin && <Notice tone="info">Peran VIEWER hanya dapat membaca. Pembuatan dan persetujuan rencana dilakukan SUPER_ADMIN.</Notice>}
      {showForm && isAdmin && (
        <div className="mb-4">
          <NewPlanForm onDone={() => setShowForm(false)} />
        </div>
      )}
      {(q.data?.needs_decision ?? 0) > 0 && (
        <div className="mb-4 flex items-center gap-2 rounded-md border border-warn/40 bg-warn-bg p-3 text-sm text-warn" role="status">
          <AlertTriangle className="h-4 w-4" aria-hidden />
          {q.data?.needs_decision} rencana menunggu keputusan Anda.
          <Link to="/decisions" className="underline">
            Buka antrean keputusan
          </Link>
        </div>
      )}
      <div className="mb-3 flex flex-wrap items-center gap-4 text-sm">
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={onlyMine} onChange={(e) => setOnlyMine(e.target.checked)} /> Hanya yang perlu keputusan
        </label>
        {projectId && (
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={scopeProject} onChange={(e) => setScopeProject(e.target.checked)} /> Hanya proyek terpilih
          </label>
        )}
      </div>
      {q.isLoading && <Loading />}
      <ErrorBox error={q.error} />
      {q.data && !rows.length && (
        <EmptyState title={onlyMine ? "Tidak ada rencana yang menunggu keputusan" : `Belum ada rencana di domain ${domain}`}>
          Buat proyek di Portofolio, lalu klik <strong>Rencana baru</strong> — atau via CLI:{" "}
          <Mono>aeos plan create --project &lt;key&gt; --domain {domain ?? "PERSONAL"} --title "..." --prompt "..." --actor &lt;username&gt;</Mono>
        </EmptyState>
      )}
      {rows.length > 0 && (
        <Table
          rows={rows}
          rowKey={(p) => p.id}
          columns={[
            {
              header: "Rencana",
              cell: (p) => (
                <div className={clsx(p.needs_decision && "font-semibold")}>
                  <Link to={`/plans/${p.id}`} className="hover:underline">
                    {p.title}
                  </Link>
                  {p.needs_decision && (
                    <div className="flex items-center gap-1 text-xs font-normal text-warn">
                      <AlertTriangle className="h-3 w-3" aria-hidden /> Perlu keputusan Anda
                    </div>
                  )}
                  {p.running && <div className="text-xs font-normal text-info">Pipeline sedang berjalan</div>}
                </div>
              ),
            },
            { header: "Proyek", cell: (p) => p.project_name ?? <Mono>{p.project_id.slice(0, 8)}</Mono> },
            { header: "Status", cell: (p) => <PlanStateBadge state={p.state} /> },
            {
              header: "Versi saat ini",
              cell: (p) =>
                p.current_version ? (
                  <span>
                    v{p.current_version.version} <Mono>{shortHash(p.current_version.manifest_hash)}</Mono>
                    {p.current_version.mode !== "LIVE" && <span className="ml-1 text-xs text-mock">{p.current_version.mode}</span>}
                  </span>
                ) : (
                  "—"
                ),
            },
            {
              header: "Versi disetujui",
              cell: (p) => (p.approved_version ? <span>v{p.approved_version.version} <Mono>{shortHash(p.approved_version.manifest_hash)}</Mono></span> : "—"),
            },
            { header: "Diperbarui", cell: (p) => <span title={p.updated_at ?? ""}>{relativeTime(p.updated_at)}</span> },
          ]}
        />
      )}
    </div>
  );
}
